# SPDX-License-Identifier: MIT
# Copyright (c) 2020 The Authors.

# Authors: Hong Chang   <@Hong-Chang>

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:The above copyright
# notice and this permission notice shall be included in all copies or
# substantial portions of the Software.THE SOFTWARE IS PROVIDED "AS IS",
# WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
# TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
# THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import logging
from cidr_trie import PatriciaTrie
from mizar.dp.mizar.operators.endpoints.endpoints_operator import *
from mizar.dp.mizar.operators.networkpolicies.networkpolicies_operator import *

endpoint_opr = EndpointOperator()
networkpolicy_opr = NetworkPolicyOperator()

logger = logging.getLogger()


class NetworkPolicyUtil:
    def handle_networkpolicy_create_update(self, name, pod_label_dict, policy_types):
        networkpolicy = networkpolicy_opr.get_or_create_networkpolicy_from_store(name)

        pods = kube_list_pods_by_labels(networkpolicy_opr.core_api, pod_label_dict)
        if pods is None:
            return

        has_ingress = "Ingress" in policy_types
        has_egress = "Egress" in policy_types

        if not has_ingress and not has_egress:
            return

        for pod in pods.items:
            pod_name = pod.metadata.name
            eps = endpoint_opr.store.get_eps_in_pod(pod_name)
            for ep in eps.values():
                networkpolicy.add_endpoint(ep.name)
                if has_ingress:
                    if name not in ep.ingress_networkpolicies:
                        ep.add_ingress_networkpolicy(name)
                    data_for_networkpolicy_ingress = self.generate_data_for_networkpolicy_ingress(ep)
                if has_egress:
                    if name not in ep.egress_networkpolicies:
                        ep.add_egress_networkpolicy(name)
                    data_for_networkpolicy_egress = self.generate_data_for_networkpolicy_egress(ep)
                data_for_networkpolicy = {
                    "old": {},
                    "ingress": data_for_networkpolicy_ingress,
                    "egress": data_for_networkpolicy_egress,
                }
                logger.info("data_for_networkpolicy: {}".format(data_for_networkpolicy))
                old_data_for_networkpolicy = ep.get_data_for_networkpolicy()
                if len(old_data_for_networkpolicy) > 0:
                    if len(old_data_for_networkpolicy["old"]) > 0 and old_data_for_networkpolicy["old"]["ingress"] == data_for_networkpolicy_ingress and old_data_for_networkpolicy["old"]["egress"] == data_for_networkpolicy_egress:
                        continue

                    old_data_for_networkpolicy["old"] = {}
                    data_for_networkpolicy["old"] = old_data_for_networkpolicy

                ep.set_data_for_networkpolicy(data_for_networkpolicy)
                ep.update_networkpolicy_per_endpoint(data_for_networkpolicy)
                for label in data_for_networkpolicy["ingress"]["label_networkpolicies_map"]:
                    networkpolicy_opr.store.add_label_networkpolicy_ingress(label, data_for_networkpolicy["ingress"]["label_networkpolicies_map"][label])
                for label in data_for_networkpolicy["egress"]["label_networkpolicies_map"]:
                    networkpolicy_opr.store.add_label_networkpolicy_egress(label, data_for_networkpolicy["egress"]["label_networkpolicies_map"][label])
        networkpolicy_opr.store_update(networkpolicy)

    def generate_data_for_networkpolicy_ingress(self, ep):
        data = self.init_data_for_networkpolicy()
        direction = "ingress"

        for networkpolicy_name in ep.ingress_networkpolicies:
            networkpolicy_spec = networkpolicy_opr.get_networkpolicy_from_cluster(networkpolicy_name)
            self.fill_data_from_directional_traffic_rules(data, direction, networkpolicy_spec)
        self.build_access_rules(data, ep)
        return data

    def generate_data_for_networkpolicy_egress(self, ep):
        data = self.init_data_for_networkpolicy()
        direction = "egress"

        for networkpolicy_name in ep.egress_networkpolicies:
            networkpolicy_spec = networkpolicy_opr.get_networkpolicy_from_cluster(networkpolicy_name)
            self.fill_data_from_directional_traffic_rules(data, direction, networkpolicy_spec)
        self.build_access_rules(data, ep)
        return data

    def init_data_for_networkpolicy(self):
        data = {
            "indexed_policy_count": 0,
            "networkpolicy_map": {},
            "cidrs_map_no_except": {},
            "cidrs_map_with_except": {},
            "cidrs_map_except": {},
            "ports_map": {},
            "label_networkpolicies_map": {},
            "cidr_and_policies_map_no_except": {},
            "cidr_and_policies_map_with_except": {},
            "cidr_and_policies_map_except": {},
            "port_and_policies_map": {},
            "indexed_policy_map": {},
            "cidr_table_no_except": [],
            "cidr_table_with_except": [],
            "cidr_table_except": [],
            "port_table": [],
        }
        return data

    def fill_data_from_directional_traffic_rules(self, data, direction, networkpolicy_spec):
        policy_name = networkpolicy_spec["metadata"]["name"]
        for index, directional_traffic_rules in enumerate(networkpolicy_spec["spec"][direction]):
            indexed_policy_name = "{}_{}_{}".format(policy_name, direction, index)
            if policy_name not in data["networkpolicy_map"]:
                data["networkpolicy_map"][policy_name] = set()
            if indexed_policy_name not in data["networkpolicy_map"][policy_name]:
                data["networkpolicy_map"][policy_name].add(indexed_policy_name)
                data["indexed_policy_count"] += 1

            self.fill_cidrs_from_directional_traffic_rules(data, policy_name, indexed_policy_name, direction, directional_traffic_rules)

    def fill_cidrs_from_directional_traffic_rules(self, data, policy_name, indexed_policy_name, direction, directional_traffic_rules):
        if indexed_policy_name not in data["ports_map"]:
            data["ports_map"][indexed_policy_name] = []
        for port in directional_traffic_rules["ports"]:
            data["ports_map"][indexed_policy_name].append("{}:{}".format(port["protocol"], port["port"]))

        if indexed_policy_name not in data["cidrs_map_no_except"]:
            data["cidrs_map_no_except"][indexed_policy_name] = []
        if indexed_policy_name not in data["cidrs_map_with_except"]:
            data["cidrs_map_with_except"][indexed_policy_name] = []
        if indexed_policy_name not in data["cidrs_map_except"]:
            data["cidrs_map_except"][indexed_policy_name] = []
        for rule_item in directional_traffic_rules["from" if direction == "ingress" else "to"]:
            if "ipBlock" in rule_item:
                if "except" in rule_item["ipBlock"]:
                    data["cidrs_map_with_except"][indexed_policy_name].append(rule_item["ipBlock"]["cidr"])
                    for except_cidr in rule_item["ipBlock"]["except"]:
                        data["cidrs_map_except"][indexed_policy_name].append(except_cidr)
                else:
                    data["cidrs_map_no_except"][indexed_policy_name].append(rule_item["ipBlock"]["cidr"])
            elif "namespaceSelector" in rule_item and "podSelector" in rule_item:
                self.add_label_networkpolicy(data, rule_item["podSelector"]["matchLabels"], policy_name)
                namespaces = kube_list_namespaces_by_labels(networkpolicy_opr.core_api, rule_item["namespaceSelector"]["matchLabels"])
                if namespaces is not None:
                    namespace_set = set()
                    for namespace in namespaces.items:
                        namespace_set.add(namespace.metadata.name)

                    pods = kube_list_pods_by_labels(networkpolicy_opr.core_api, rule_item["podSelector"]["matchLabels"])
                    if pods is not None:
                        for pod in pods.items:
                            if pod.metadata.namespace in namespace_set and pod.status.pod_ip is not None:
                                data["cidrs_map_no_except"][indexed_policy_name].append("{}/32".format(pod.status.pod_ip))
            elif "namespaceSelector" in rule_item:                
                namespaces = kube_list_namespaces_by_labels(networkpolicy_opr.core_api, rule_item["namespaceSelector"]["matchLabels"])
                if namespaces is not None:
                    for namespace in namespaces.items:
                        pods = kube_list_pods_by_namespace(networkpolicy_opr.core_api, namespace.metadata.name)
                        if pods is not None:
                            for pod in pods.items:
                                if pod.status.pod_ip is not None:
                                    data["cidrs_map_no_except"][indexed_policy_name].append("{}/32".format(pod.status.pod_ip))
            elif "podSelector" in rule_item:
                self.add_label_networkpolicy(data, rule_item["podSelector"]["matchLabels"], policy_name)
                pods = kube_list_pods_by_labels(networkpolicy_opr.core_api, rule_item["podSelector"]["matchLabels"])
                if pods is not None:
                    for pod in pods.items:
                        if pod.status.pod_ip is not None:
                            data["cidrs_map_no_except"][indexed_policy_name].append("{}/32".format(pod.status.pod_ip))
            else:
                raise NotImplementedError("Not implemented for {}".format(rule_item))

    def add_label_networkpolicy(self, data, label_dict, policy_name):
        for key in label_dict:
            label = "{}={}".format(key, label_dict[key])
            if label not in data["label_networkpolicies_map"]:
                data["label_networkpolicies_map"][label] = set()
            data["label_networkpolicies_map"][label].add(policy_name)

    def build_access_rules(self, access_rules, ep):
        self.build_cidr_and_policies_map(access_rules, "no_except")
        self.build_cidr_and_policies_map(access_rules, "with_except")
        self.build_cidr_and_policies_map(access_rules, "except")
        self.build_port_and_policies_map(access_rules)
        self.build_indexed_policy_map(access_rules)
        self.build_cidr_table(access_rules, ep, "no_except")
        self.build_cidr_table(access_rules, ep, "with_except")
        self.build_cidr_table(access_rules, ep, "except")
        self.build_port_table(access_rules, ep)

    def build_cidr_and_policies_map(self, access_rules, cidr_type):
        cidr_map_name = "cidrs_map_" + cidr_type
        cidr_and_policies_map_name = "cidr_and_policies_map_" + cidr_type
        trie = PatriciaTrie()
        for indexed_policy_name, cidrs in access_rules[cidr_map_name].items():
            for cidr in cidrs:
                if cidr not in access_rules[cidr_and_policies_map_name]:
                    access_rules[cidr_and_policies_map_name][cidr] = set()
                access_rules[cidr_and_policies_map_name][cidr].add(indexed_policy_name)
                trie.insert(cidr, access_rules[cidr_and_policies_map_name][cidr])
        for cidr, indexed_policy_names in access_rules[cidr_and_policies_map_name].items():
            found_cidr_map = trie.find_all(cidr)
            for found_cidr_tuple in found_cidr_map:
                if indexed_policy_names != found_cidr_tuple[1]:
                    for foundPolicyName in found_cidr_tuple[1]:
                        indexed_policy_names.add(foundPolicyName)

    def build_port_and_policies_map(self, access_rules):
        for indexed_policy_name, ports in access_rules["ports_map"].items():
            for port in ports:
                if port not in access_rules["port_and_policies_map"]:
                    access_rules["port_and_policies_map"][port] = set()
                access_rules["port_and_policies_map"][port].add(indexed_policy_name)

    def build_indexed_policy_map(self, access_rules):
        bit = 1
        for _, indexed_policy_names in access_rules["networkpolicy_map"].items():
            for indexed_policy_name in indexed_policy_names:
                access_rules["indexed_policy_map"][indexed_policy_name] = bit
                bit <<= 1

    def build_cidr_table(self, access_rules, ep, cidr_type):
        cidr_table_name = "cidr_table_" + cidr_type
        cidr_and_policies_map_name = "cidr_and_policies_map_" + cidr_type
        for cidr, indexed_policy_names in access_rules[cidr_and_policies_map_name].items():
            splitted_cidr = cidr.split("/")
            access_rules[cidr_table_name].append({
                "vni": ep.vni,
                "local_ip": ep.ip,
                "cidr": splitted_cidr[0],
                "cidr_length": int(splitted_cidr[1]),
                "bit_value": self.calculate_policy_bit_value(access_rules, indexed_policy_names),
            })

    def calculate_policy_bit_value(self, access_rules, indexed_policy_names):
        policy_bit_value = 0
        for indexed_policy_name in indexed_policy_names:
            policy_bit_value += access_rules["indexed_policy_map"][indexed_policy_name]
        return policy_bit_value

    def build_port_table(self, access_rules, ep):
        for port, indexed_policy_names in access_rules["port_and_policies_map"].items():
            splitted = port.split(":")
            access_rules["port_table"].append({
                "vni": ep.vni,
                "local_ip": ep.ip,
                "protocol": splitted[0],
                "port": splitted[1],
                "bit_value": self.calculate_policy_bit_value(access_rules, indexed_policy_names),
            })

    def handle_pod_change_for_networkpolicy(self, diff):
        data = self.extract_pod_label_change(diff)
        if len(data["add"]) == 0 and len(data["remove"]) == 0:
            return

        policy_name_list = set()
        for label in data["add"]:
            self.add_affected_networkpolicy_by_pod_label(policy_name_list, label)

        for label in data["remove"]:
            self.add_affected_networkpolicy_by_pod_label(policy_name_list, label)

        if len(policy_name_list) == 0:
            return

        # TODO Trigger networkpolicy update by policy_name_list

    def add_affected_networkpolicy_by_pod_label(self, policy_name_list, label):
        if label in networkpolicy_opr.store.label_networkpolicies_ingress_store:
            for policy_name in networkpolicy_opr.store.label_networkpolicies_ingress_store[label]:
                policy_name_list.add(policy_name)
        if label in networkpolicy_opr.store.label_networkpolicies_egress_store:
            for policy_name in networkpolicy_opr.store.label_networkpolicies_egress_store[label]:
                policy_name_list.add(policy_name)

    def extract_pod_label_change(self, diff):
        data = {
            "add": set(),
            "remove": set()
        }
        for item in diff:
            self.process_pod_label_change(data, item[0], item[1], item[2], item[3])

        return data        

    def process_pod_label_change(self, data, change_type, field, old, new):
        if field is not None and len(field) == 3 and field[0] == "metadata" and field[1] == "labels":
            if change_type == "add":
                data["add"].add("{}={}".format(field[2], new))
            elif change_type == "remove":
                data["remove"].add("{}={}".format(field[2], old))
            elif change_type == "change":
                data["add"].add("{}={}".format(field[2], new))
                data["remove"].add("{}={}".format(field[2], old))
            else:
                raise NotImplementedError("Not implemented for label change type of {}".format(change_type))
        elif field is not None and len(field) == 0:
            if change_type == "add" and new["metadata"] is not None and new["metadata"]["labels"] is not None:
                labels = new["metadata"]["labels"]
                for key in labels:
                    data["add"].add("{}={}".format(key, labels[key]))
            elif change_type == "remove" and old["metadata"] is not None and old["metadata"]["labels"] is not None:
                labels = old["metadata"]["labels"]
                for key in labels:
                    data["remove"].add("{}={}".format(key, labels[key]))
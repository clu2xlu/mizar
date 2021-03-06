// SPDX-License-Identifier: GPL-2.0-or-later
/**
 * @file trn_transit_xdp.c
 * @author Sherif Abdelwahab (@zasherif)
 *
 * @brief Implements the Transit XDP program (switching and routing logic)
 *
 * @copyright Copyright (c) 2019 The Authors.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; version 2 of the License.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 *
 */

#include <linux/bpf.h>
#include <linux/if_arp.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <linux/if_vlan.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>
#include <linux/socket.h>
#include <linux/tcp.h>
#include <linux/types.h>
#include <linux/udp.h>
#include <stddef.h>
#include <string.h>

#include "extern/bpf_endian.h"
#include "extern/bpf_helpers.h"

#include "trn_datamodel.h"
#include "trn_kern.h"
#include "trn_transit_xdp_stages_maps.h"

int _version SEC("version") = 1;

static __inline int trn_scaled_ep_decide(struct transit_packet *pkt)
{
	void *endpoints_map;
	struct endpoint_t *ep;
	struct endpoint_key_t epkey;
	int map_idx = 0;
	__u32 remote_idx;
	__be64 tunnel_id = trn_vni_to_tunnel_id(pkt->geneve->vni);

	endpoints_map = bpf_map_lookup_elem(&endpoints_map_ref, &map_idx);
	if (!endpoints_map) {
		bpf_debug("[Scaled_EP:%d:] failed to find endpoints_map\n",
			  __LINE__);
		return XDP_ABORTED;
	}

	__builtin_memcpy(&epkey.tunip[0], &tunnel_id, sizeof(tunnel_id));
	epkey.tunip[2] = pkt->inner_ipv4_tuple.daddr;

	/* Get the scaled endpoint configuration */
	ep = bpf_map_lookup_elem(endpoints_map, &epkey);

	if (!ep) {
		bpf_debug(
			"[Scaled_EP:%d:] (BUG) failed to find scaled endpoint configuration\n",
			__LINE__);
		return XDP_ABORTED;
	}

	/* Simple hashing for now! */
	__u32 inhash =
		jhash_2words(pkt->inner_ipv4_tuple.saddr,
			     pkt->inner_ipv4_tuple.sport, INIT_JHASH_SEED);

	if (ep->nremote_ips == 0) {
		bpf_debug(
			"[Scaled_EP] DROP: no backend attached to scaled endpoint 0x%x!\n",
			bpf_ntohl(pkt->inner_ipv4_tuple.daddr));
		return XDP_DROP;
	}

	remote_idx = inhash % ep->nremote_ips;

	if (remote_idx > TRAN_MAX_REMOTES - 1) {
		bpf_debug(
			"[Scaled_EP] DROP (BUG): Selected remote index [%d] "
			"is greater than maximum number of supported remote endpoints [%d]!\n",
			remote_idx, TRAN_MAX_REMOTES);
		return XDP_ABORTED;
	}

	pkt->scaled_ep_opt->opt_class = TRN_GNV_OPT_CLASS;
	pkt->scaled_ep_opt->type = TRN_GNV_SCALED_EP_OPT_TYPE;
	pkt->scaled_ep_opt->length = sizeof(struct trn_gnv_scaled_ep_data) / 4;
	pkt->scaled_ep_opt->scaled_ep_data.msg_type = TRN_SCALED_EP_MODIFY;

	pkt->scaled_ep_opt->scaled_ep_data.target.daddr =
		ep->remote_ips[remote_idx];

	pkt->scaled_ep_opt->scaled_ep_data.target.saddr =
		pkt->inner_ipv4_tuple.saddr;

	pkt->scaled_ep_opt->scaled_ep_data.target.sport =
		pkt->inner_ipv4_tuple.sport;

	pkt->scaled_ep_opt->scaled_ep_data.target.dport =
		pkt->inner_ipv4_tuple.dport;

	__builtin_memcpy(&pkt->scaled_ep_opt->scaled_ep_data.target.h_source,
			 pkt->inner_eth->h_source,
			 ETH_ALEN * sizeof(pkt->inner_eth->h_source[0]));

	__builtin_memcpy(&pkt->scaled_ep_opt->scaled_ep_data.target.h_dest,
			 pkt->inner_eth->h_dest,
			 ETH_ALEN * sizeof(pkt->inner_eth->h_dest[0]));

	/*Reset rts for now, todo: add endpoint host in an rts opt to minimize hop counts*/
	trn_reset_rts_opt(pkt);

	trn_set_src_dst_ip_csum(pkt, pkt->ip->daddr, pkt->ip->saddr);

	trn_swap_src_dst_mac(pkt->data);

	bpf_debug("[Scaled_EP:%d:] ** scaled endpoint to 0x%x!!\n", __LINE__,
		  bpf_ntohl(pkt->scaled_ep_opt->scaled_ep_data.target.daddr));
	return XDP_TX;
}

static __inline int trn_sep_process_inner_udp(struct transit_packet *pkt)
{
	pkt->inner_udp = (void *)pkt->inner_ip + sizeof(*pkt->inner_ip);

	if (pkt->inner_udp + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	bpf_debug("[Scaled_EP:%d:0x%x] Process UDP \n", __LINE__,
		  bpf_ntohl(pkt->itf_ipv4));

	pkt->inner_ipv4_tuple.sport = pkt->inner_udp->source;
	pkt->inner_ipv4_tuple.dport = pkt->inner_udp->dest;

	return trn_scaled_ep_decide(pkt);
}

static __inline int trn_sep_process_inner_tcp(struct transit_packet *pkt)
{
	pkt->inner_tcp = (void *)pkt->inner_ip + sizeof(*pkt->inner_ip);

	if (pkt->inner_tcp + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	pkt->inner_ipv4_tuple.sport = pkt->inner_tcp->source;
	pkt->inner_ipv4_tuple.dport = pkt->inner_tcp->dest;

	bpf_debug("[Scaled_EP:%d:0x%x] Process TCP\n", __LINE__,
		  bpf_ntohl(pkt->itf_ipv4));

	return trn_scaled_ep_decide(pkt);
}

static __inline int trn_sep_process_inner_icmp(struct transit_packet *pkt)
{
	bpf_debug(
		"[Scaled_EP:%d:] scaled endpoint 0x%x does not handle ICMP!!\n",
		__LINE__, bpf_ntohl(pkt->inner_ip->daddr));

	// TODO: return XDP_DROP
	pkt->inner_ipv4_tuple.sport = 0;
	pkt->inner_ipv4_tuple.dport = 0;

	return trn_scaled_ep_decide(pkt);
}

static __inline int trn_sep_process_inner_ip(struct transit_packet *pkt)
{
	pkt->inner_ip = (void *)pkt->inner_eth + pkt->inner_eth_off;

	if (pkt->inner_ip + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	pkt->inner_ipv4_tuple.saddr = pkt->inner_ip->saddr;
	pkt->inner_ipv4_tuple.daddr = pkt->inner_ip->daddr;
	pkt->inner_ipv4_tuple.protocol = pkt->inner_ip->protocol;

	if (pkt->inner_ipv4_tuple.protocol == IPPROTO_UDP) {
		return trn_sep_process_inner_udp(pkt);
	}

	if (pkt->inner_ipv4_tuple.protocol == IPPROTO_TCP) {
		return trn_sep_process_inner_tcp(pkt);
	}

	if (pkt->inner_ipv4_tuple.protocol == IPPROTO_ICMP) {
		return trn_sep_process_inner_icmp(pkt);
	}

	bpf_debug("[Scaled_EP:%d:0x%x] Unsupported inner protocol \n", __LINE__,
		  bpf_ntohl(pkt->itf_ipv4));

	return XDP_DROP;
}

static __inline int trn_sep_process_inner_eth(struct transit_packet *pkt)
{
	pkt->inner_eth = (void *)pkt->geneve + pkt->gnv_hdr_len;
	pkt->inner_eth_off = sizeof(*pkt->inner_eth);

	if (pkt->inner_eth + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	if (pkt->eth->h_proto != bpf_htons(ETH_P_IP)) {
		bpf_debug(
			"[Scaled_EP:%d:0x%x] DROP: unsupported inner packet: [0x%x]\n",
			__LINE__, bpf_ntohl(pkt->itf_ipv4),
			bpf_ntohs(pkt->eth->h_proto));
		return XDP_DROP;
	}

	return trn_sep_process_inner_ip(pkt);
}

static __inline int trn_sep_process_geneve(struct transit_packet *pkt)
{
	pkt->geneve = (void *)pkt->udp + sizeof(*pkt->udp);
	if (pkt->geneve + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	pkt->gnv_opt_len = pkt->geneve->opt_len * 4;
	pkt->gnv_hdr_len = sizeof(*pkt->geneve) + pkt->gnv_opt_len;
	pkt->rts_opt = (void *)&pkt->geneve->options[0];

	if (pkt->rts_opt + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	pkt->scaled_ep_opt = (void *)pkt->rts_opt + sizeof(*pkt->rts_opt);

	if (pkt->scaled_ep_opt + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	return trn_sep_process_inner_eth(pkt);
}

static __inline int trn_sep_process_udp(struct transit_packet *pkt)
{
	/* Get the UDP header */
	pkt->udp = (void *)pkt->ip + sizeof(*pkt->ip);

	if (pkt->udp + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	return trn_sep_process_geneve(pkt);
}

static __inline int trn_sep_process_ip(struct transit_packet *pkt)
{
	/* Get the IP header */
	pkt->ip = (void *)pkt->eth + pkt->eth_off;

	if (pkt->ip + 1 > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	if (!pkt->ip->ttl)
		return XDP_DROP;

	return trn_sep_process_udp(pkt);
}

static __inline int trn_sep_process_eth(struct transit_packet *pkt)
{
	pkt->eth = pkt->data;
	pkt->eth_off = sizeof(*pkt->eth);

	if (pkt->data + pkt->eth_off > pkt->data_end) {
		bpf_debug("[Scaled_EP:%d:0x%x] ABORTED: Bad offset\n", __LINE__,
			  bpf_ntohl(pkt->itf_ipv4));
		return XDP_ABORTED;
	}

	return trn_sep_process_ip(pkt);
}

SEC("transit_scaled_endpoint")
int _transit_scaled_endpoint(struct xdp_md *ctx)
{
	/* Simple scaled endpoint implementation */

	void *interface_config_map;
	struct transit_packet pkt;
	int map_idx = 0;
	pkt.data = (void *)(long)ctx->data;
	pkt.data_end = (void *)(long)ctx->data_end;
	pkt.xdp = ctx;

	struct tunnel_iface_t *itf;

	interface_config_map =
		bpf_map_lookup_elem(&interface_config_map_ref, &map_idx);
	if (!interface_config_map) {
		bpf_debug(
			"[Scaled_EP:%d:] failed to find interface_config_map\n",
			__LINE__);
		return XDP_ABORTED;
	}

	int k = 0;
	itf = bpf_map_lookup_elem(interface_config_map, &k);

	if (!itf) {
		bpf_debug("[Scaled_EP:%d:] ABORTED: Bad configuration\n",
			  __LINE__);
		return XDP_ABORTED;
	}

	pkt.itf_ipv4 = itf->ip;
	pkt.itf_idx = itf->iface_index;

	return trn_sep_process_eth(&pkt);
}

char _license[] SEC("license") = "GPL";

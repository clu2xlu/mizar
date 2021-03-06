import logging
import random
from common.constants import *
from common.common import *
from kubernetes import client, config
from obj.droplet import Droplet
from obj.bouncer import Bouncer
from obj.divider import Divider
from store.operator_store import OprStore

logger = logging.getLogger()

class DropletOperator(object):
	_instance = None

	def __new__(cls, **kwargs):
		if cls._instance is None:
			cls._instance = super(DropletOperator, cls).__new__(cls)
			cls._init(cls, **kwargs)
		return cls._instance

	def _init(self, **kwargs):
		logger.info(kwargs)
		self.store = OprStore()
		config.load_incluster_config()
		self.obj_api = client.CustomObjectsApi()
		self.bootstrapped = False

	def query_existing_droplets(self):
		def list_droplet_obj_fn(name, spec, plurals):
			logger.info("Bootstrapped droplet {}".format(name))
			d = Droplet(name, self.obj_api, self.store, spec)
			if d.status == OBJ_STATUS.droplet_status_provisioned:
				self.store_update(d)

		kube_list_obj(self.obj_api, RESOURCES.droplets, list_droplet_obj_fn)
		self.bootstrapped = True

	def is_bootstrapped(self):
		return self.bootstrapped

	def get_droplet_tmp_obj(self, name, spec):
		return Droplet(name, self.obj_api, None, spec)

	def get_droplet_stored_obj(self, name, spec):
		return Droplet(name, self.obj_api, self.store, spec)

	def set_droplet_provisioned(self, droplet):
		droplet.set_status(OBJ_STATUS.droplet_status_provisioned)
		droplet.update_obj()

	def store_update(self, droplet):
		self.store.update_droplet(droplet)

	def on_droplet_provisioned(self, body, spec, **kwargs):
		name = kwargs['name']
		logger.info("Droplet on_droplet_provisioned {} with spec: {}".format(name, spec))
		d = Droplet(name, self.obj_api, self.store, spec)
		self.store.update_droplet(d)

	def on_bouncer_init(self, body, spec, **kwargs):
		name = kwargs['name']
		logger.info("Droplet place bouncer {} with spec: {}".format(name, spec))
		b = Bouncer(name, self.obj_api, None, spec)
		self.assign_bouncer_droplet(b)
		b.set_status(OBJ_STATUS.bouncer_status_placed)
		b.update_obj()

	def assign_bouncer_droplet(self, bouncer):
		droplets = set(self.store.get_all_droplets())
		d = random.sample(droplets, 1)[0]
		bouncer.set_droplet(d)

	def assign_divider_droplet(self, divider):
		droplets = set(self.store.get_all_droplets())
		d = random.sample(droplets, 1)[0]
		divider.set_droplet(d)

	def on_delete(self, body, spec, **kwargs):
		name = kwargs['name']
		logger.info("*delete_droplet {}".format(name))
		#self.ds.delete(name)



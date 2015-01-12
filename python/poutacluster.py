#!/usr/bin/env python

"""
Poutacluster - helper script to set up a cluster in pouta.csc.fi

@author: Olli Tourunen
"""

import os
import shlex
import subprocess
import sys
import yaml
import time
import datetime
import openstack_api_wrapper as oaw

"""
Class to represent a cluster instance with one frontend and multiple nodes
"""


class Cluster(object):
    name = None
    config = None
    frontend = None
    nodes = []
    volumes = []
    nova_client = None
    cinder_client = None

    def __init__(self, config, nova_client, cinder_client):
        self.config = config
        self.nova_client = nova_client
        self.cinder_client = cinder_client
        self.name = config['cluster']['name']
        self.__provisioning_log = []

    def __prov_log(self, action, resource_type, resource_id, info=''):
        entry = {'time': datetime.datetime.now().isoformat(),
                 'action': action, 'resource_type': resource_type, 'resource_id': '%s' % resource_id, 'info': info}
        self.__provisioning_log.append(entry)

    def __provision_vm(self, name, sec_groups, spec, network, server_group_name=None):
        image_id = oaw.check_image_exists(self.nova_client, spec['image'])
        flavor_id = oaw.check_flavor_exists(self.nova_client, spec['flavor'])
        server_group_id = None
        if server_group_name:
            server_group_id = oaw.check_server_group_exists(self.nova_client, server_group_name, ['anti-affinity'])

        # network needs more logic. We accept the magic keyword 'default', which will then try to use the tenant's
        # default network labeled after the tenant name
        if network == 'default':
            network = os.environ['OS_TENANT_ID']
        network_id = oaw.check_network_exists(self.nova_client, network)

        print '    creating %s: %s  - %s' % (name, spec['image'], spec['flavor'])
        print "    using network '%s'" % network
        instance_id = oaw.create_vm(self.nova_client, name, image_id, flavor_id, spec['sec-key'], sec_groups,
                                    network_id, server_group_id)

        print '    instance %s created' % instance_id
        self.__prov_log('create', 'vm', instance_id, name)

        instance = oaw.get_instance(self.nova_client, instance_id)

        return instance

    def __provision_vm_addresses(self, instance, spec):

        print '    instance internal IP: %s' % oaw.get_addresses(instance)[0]
        if 'public-ip' in spec.keys():
            ip = spec['public-ip']
            print "    associating public IP %s" % ip
            instance.add_floating_ip(ip)


    def __provision_volumes(self, instance, volspec):
        vd = chr(ord('c'))
        for volconf in volspec:
            vol_name = '%s/%s' % (instance.name, volconf['name'])
            vol_size = volconf['size']
            device = '/dev/vd%s' % vd

            # check if there is already a volume or do we need to create one
            ex_vol = None
            for vol in self.volumes:
                if vol.display_name == vol_name:
                    ex_vol = vol
                    break
            if ex_vol:
                # we have an existing volume, let's check if we need to attach it
                if instance.id in [x['server_id'] for x in ex_vol.attachments]:
                    print "    volume %s already attached" % vol_name
                else:
                    print "    attaching existing volume %s with size %s as device %s" % (
                        ex_vol.display_name, ex_vol.size, device)
                    oaw.attach_volume(self.nova_client, self.cinder_client, instance, ex_vol, device, async=True)

            else:
                print "    creating and attaching volume %s with size %s as device %s" % (vol_name, vol_size, device)
                vol = oaw.create_and_attach_volume(self.nova_client, self.cinder_client, {}, instance,
                                                   vol_name, vol_size, dev=device, async=True)
                self.__prov_log('create', 'volume', vol.id, vol_name)

            vd = chr(ord(vd) + 1)

    def __provision_sec_groups(self):

        # first external access group for frontend
        sg_name_ext = self.name + '-ext'

        try:
            oaw.check_secgroup_exists(self.nova_client, sg_name_ext)
        except RuntimeError:
            print
            print 'No security group for external access exists, creating an empty placeholder'
            print 'NOTE: you will need to add the rules afterwards through '
            print
            print '      nova secgroup-add-rule %s ...' % sg_name_ext
            print
            print '      or through the web interface'
            print
            sg = oaw.create_sec_group(self.nova_client, sg_name_ext,
                                      'Security group for %s external access' % self.name)
            self.__prov_log('create', 'sec-group', sg.id, sg.name)

        # then the cluster internal group
        sg_name_int = self.name + '-int'

        try:
            oaw.check_secgroup_exists(self.nova_client, sg_name_int)
        except RuntimeError:
            print 'No security group for internal access exists, creating it'
            sg = oaw.create_sec_group(self.nova_client, sg_name_int,
                                      'Security group for %s internal access' % self.name)
            self.__prov_log('create', 'sec-group', sg.id, sg.name)

            # add inter-cluster access
            oaw.create_local_access_rules(self.nova_client, sg_name_int, sg_name_int)
            # add access from other security groups (usually 'bastion')
            for sg in self.config['cluster']['allow-traffic-from-sec-groups']:
                oaw.create_local_access_rules(self.nova_client, sg_name_int, sg)

    def __provision_server_group(self):

        try:
            oaw.check_server_group_exists(self.nova_client, self.name, ['anti-affinity'])
        except RuntimeError:
            print
            print 'No server group for %s exists, creating one' % self.name
            sg_id = oaw.create_server_group(self.nova_client, self.name, ['anti-affinity'])
            self.__prov_log('create', 'server-group', sg_id, self.name)

    def __provision_frontend(self):
        fe_name = self.name + '-fe'

        if self.frontend:
            print '    %s already provisioned' % fe_name
        else:
            self.frontend = self.__provision_vm(fe_name, [self.name + '-ext', self.name + '-int'],
                                                self.config['frontend'],
                                                self.config['cluster']['network'],
                                                server_group_name=self.name)

        oaw.wait_for_state(self.nova_client, 'servers', self.frontend.id, 'ACTIVE')
        # reload information after instance has reached active state
        self.frontend = oaw.get_instance(self.nova_client, self.frontend.id)
        self.__provision_vm_addresses(self.frontend, self.config['frontend'])
        self.__provision_volumes(self.frontend, self.config['frontend']['volumes'])

    def __provision_nodes(self, num_nodes):
        node_base = self.name + '-node'

        # asynchronous part
        for i in range(1, num_nodes + 1):
            node_name = '%s%02d' % (node_base, i)
            node = None
            for n in self.nodes:
                if n.name == node_name:
                    node = n
            if node:
                print '    %s already provisioned' % node_name
            else:
                node = self.__provision_vm(node_name, [self.name + '-int'],
                                           self.config['node'],
                                           self.config['cluster']['network'],
                                           server_group_name=self.name)
                self.nodes.append(node)
                oaw.wait_for_state(self.nova_client, 'servers', node.id, 'BUILD')

            print

        # synchronous part after nodes are active
        # indexed access because we'll replace the node instances with updated versions
        for i in range(0, len(self.nodes)):
            node = self.nodes[i]
            print '    setup network and volumes for %s' % node.name
            oaw.wait_for_state(self.nova_client, 'servers', node.id, 'ACTIVE')
            # reload information after instance has reached active state
            node = oaw.get_instance(self.nova_client, node.id)
            self.nodes[i] = node
            self.__provision_vm_addresses(node, self.config['node'])
            self.__provision_volumes(node, self.config['node']['volumes'])
            print

    def __filter_volumes_for_node(self, volumes, vm_name):
        return [x for x in volumes
                if x.display_name
                and x.display_name.startswith('%s/' % vm_name)
                and x.status in ['in-use', 'available']]

    def get_volumes_for_node(self, vm_name):
        return self.__filter_volumes_for_node(self.volumes, vm_name)

    def load_provisioned_state(self):
        print "Loading cluster state from OpenStack"

        # reset the state
        self.frontend = None
        self.nodes = []
        self.volumes = []

        vms = self.nova_client.servers.list()
        all_vols = self.cinder_client.volumes.list()

        fe_name = '%s-fe' % self.name
        existing_nodes = filter(lambda lx: lx.name == fe_name, vms)
        if len(existing_nodes) > 1:
            raise RuntimeError('More than one frontend VM with the name %s found, unable to continue' % fe_name)
        if len(existing_nodes) == 1:
            self.frontend = existing_nodes[0]
            print '    found frontend %s' % self.frontend.name

        # find volumes created for the frontend
        fe_vols = self.__filter_volumes_for_node(all_vols, fe_name)
        for vol in fe_vols:
            print "    found volume %s" % vol.display_name
        self.volumes.extend(fe_vols)

        # find cluster nodes
        node_base = '%s-node' % self.name
        for i in range(1, 100):
            node_name = '%s%02d' % (node_base, i)
            existing_nodes = filter(lambda lx: lx.name == node_name, vms)
            if len(existing_nodes) == 1:
                node = existing_nodes[0]
                print '    found node %s' % node.name
                self.nodes.append(node)
            elif len(existing_nodes) > 1:
                raise RuntimeError('More than one VM with the name %s found, unable to continue' % node_name)

            # find volumes created for the node
            node_vols = self.__filter_volumes_for_node(all_vols, node_name)
            for vol in node_vols:
                print "    found volume %s" % vol.display_name

            self.volumes.extend(node_vols)

        if not self.frontend and len(self.nodes) == 0 and len(self.volumes) == 0:
            print "    no existing resources found"

    def up(self, num_nodes):
        print
        print "Provisioning security groups"
        self.__provision_sec_groups()

        print
        print "Provisioning server group"
        self.__provision_server_group()

        print
        print "Provisioning cluster frontend"
        self.__provision_frontend()

        print
        print "Provisioning %d cluster nodes" % num_nodes
        self.__provision_nodes(num_nodes)

        print
        print "Checking volume attach state"
        for vol in self.volumes:
            print "    %s" % vol.display_name
            oaw.wait_for_state(self.cinder_client, 'volumes', vol.id, 'in-use')
            print

    def down(self):
        # take nodes down in reverse order
        for node in self.nodes[::-1]:
            print "Shutting down and deleting %s" % node.name
            oaw.shutdown_vm(self.nova_client, node)
            oaw.delete_vm(node)
            self.__prov_log('delete', 'vm', node.id, node.name)
        self.nodes = []

        # take the frontend down last
        if self.frontend:
            print "Shutting down and deleting %s " % self.frontend.name
            oaw.shutdown_vm(self.nova_client, self.frontend)
            oaw.delete_vm(self.frontend)
            self.__prov_log('delete', 'vm', self.frontend.id, self.frontend.name)
            self.frontend = None

    def destroy_volumes(self):
        if self.frontend or len(self.nodes) > 0:
            print
            print "ERROR: cluster seems to be up, refusing to delete volumes"
            print
            return

        if len(self.volumes) == 0:
            print "No volumes found to destroy"
            return

        print "Destroying all %d volumes (=persistent data) for cluster '%s'" % (len(self.volumes), self.name)
        print "Hit ctrl-c now to abort"
        print "Starting in ",
        for i in range(10, -1, -1):
            print i, " ",
            sys.stdout.flush()
            time.sleep(1)
        print ""

        # delete volumes in reverse order, frontend last
        for vol in self.volumes[::-1]:
            print "Deleting volume %s %s" % (vol.id, vol.display_name)
            oaw.delete_volume_by_id(self.cinder_client, vol.id, True)
            self.__prov_log('delete', 'volume', vol.id, vol.display_name)

    def reset_nodes(self):
        for node in self.nodes:
            print "Hard resetting %s" % node.name
            node.reboot(reboot_type='HARD')
            # rate limit resets, otherwise API will give an error
            time.sleep(3)

    def get_info(self):
        res = []

        def vm_info(vm):
            template = '%014s: %s'
            res.append(template % ('name', vm.name))
            res.append(template % ('internal ip', oaw.get_addresses(vm)[0]))
            floating_ips = oaw.get_addresses(vm, 'floating')
            if len(floating_ips) > 0:
                res.append(template % ('public ip', floating_ips[0]))
            res.append(template % ('flavor', oaw.find_flavor_name_by_id(self.nova_client, vm.flavor['id'])))
            res.append(template % ('image', oaw.find_image_name_by_id(self.nova_client, vm.image['id'])))
            res.append('%014s:' % 'volumes')
            for vol in self.get_volumes_for_node(vm.name):
                res.append('%s %s - %sGB' % (' ' * 10, vol.display_name, vol.size))
            res.append(' ')

        res.append('Frontend:')
        if self.frontend:
            vm_info(self.frontend)
        else:
            res.append('    No frontend found')

        res.append(' ')
        res.append('Nodes:')
        if len(self.nodes) > 0:
            for node in self.nodes:
                vm_info(node)
        else:
            res.append('    No nodes found')

        return res

    def generate_ansible_inventory(self):

        # noinspection PyListCreation
        lines = []
        lines.append('# WARNING: this file will be overwritten whenever cluster provisioning is run')
        if not self.frontend:
            return lines

        for group in self.config['frontend']['groups']:
            lines.append('[%s]' % group)
            name = self.frontend.name
            ip = oaw.get_addresses(self.frontend)[0]
            admin_user = self.config['frontend']['admin-user']
            lines.append('%s ansible_ssh_host=%s ansible_ssh_user=%s' % (name, ip, admin_user))
            lines.append('')

        for group in self.config['node']['groups']:
            lines.append('[%s]' % group)
            for node in self.nodes:
                name = node.name
                ip = oaw.get_addresses(node)[0]
                admin_user = self.config['node']['admin-user']
                lines.append('%s ansible_ssh_host=%s ansible_ssh_user=%s' % (name, ip, admin_user))

        return lines

    def get_provisioning_log(self):
        return self.__provisioning_log[:]


def update_ansible_inventory(cluster):
    # update ansible inventory
    with open('ansible-hosts', 'w') as f:
        for line in cluster.generate_ansible_inventory():
            f.write(line)
            f.write('\n')
    print
    print "Updated ansible inventory file 'ansible-hosts'"
    print


def check_connectivity():
    cmd = "ansible -o --sudo -i ansible-hosts '*' -a 'uname -a'"
    print cmd
    while subprocess.call(shlex.split(cmd)) != 0:
        print "    no full connectivity yet, waiting a bit and retrying"
        time.sleep(10)


def run_main_playbook():
    cmd = "ansible-playbook ../ansible/playbooks/site.yml -i ansible-hosts -f 10"
    print cmd
    while subprocess.call(shlex.split(cmd)) != 0:
        print "    problem detected in running configuration, waiting a bit and retrying."
        time.sleep(10)


def run_update_and_reboot():
    cmd = "ansible-playbook ../ansible/playbooks/update_and_reboot.yml -i ansible-hosts -f 10"
    print cmd
    subprocess.call(shlex.split(cmd))


def run_configuration():
    print
    print "Checking the connectivity again after the reboot"
    print
    check_connectivity()
    print
    print "Run the main playbook to configure the cluster"
    print
    run_main_playbook()


def run_first_time_setup():
    print
    print "First we'll check the connectivity to the cluster"
    print
    check_connectivity()
    print
    print "When all hosts are up, proceed with a system package update followed by a reboot"
    print "(this may take a while)"
    print
    run_update_and_reboot()

    print "Sleeping for a while before starting polling the hosts after the reboot"
    time.sleep(20)
    check_connectivity()
    print
    print "Run the main playbook to configure the cluster"
    print
    run_main_playbook()


def print_usage_instructions(cluster):
    frontend_public_ip = cluster.config['frontend']['public-ip']

    print "To ssh in to the the frontend:"
    print "    ssh %s@%s" % (cluster.config['frontend']['admin-user'], frontend_public_ip)
    print
    print "To check the web interfaces for Ganglia, Hadoop and Spark, browse to:"
    print "%020s : %s " % ('Ganglia', 'http://%s/ganglia/' % frontend_public_ip)
    print "%020s : %s " % ('Hadoop DFS', 'http://%s:50070/' % frontend_public_ip)
    print "%020s : %s " % ('Hadoop Map-Reduce', 'http://%s:50030/' % frontend_public_ip)
    print "%020s : %s " % ('Spark', 'http://%s:8080/' % frontend_public_ip)
    print
    print "See README.rst for examples on testing the installation"


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['up', 'down', 'info', 'reset_nodes', 'destroy_volumes', 'configure'])
    parser.add_argument(
        'num_nodes', metavar='num nodes', nargs='?', type=int, help='number of nodes')
    args, commands = parser.parse_known_args()
    command = args.command

    # get references to nova and cinder API
    nova_client, cinder_client = oaw.get_clients()

    # load the yaml configuration
    print
    print "Loading cluster definition from cluster.conf"
    with open('cluster.yml', 'r') as f:
        conf = yaml.load(f)
    print "    %12s: %s" % ('cluster name', conf['cluster']['name'])
    print "    %12s: %s" % ('description', conf['cluster']['description'])
    print

    # create Cluster instance and load state from OpenStack
    cluster = Cluster(conf, nova_client, cinder_client)
    cluster.load_provisioned_state()

    # Execute the given command

    # bring cluster up
    if command == 'up':
        if cluster.frontend:
            print "Cluster is already running"
            sys.exit(1)

        if not args.num_nodes:
            print
            print "ERROR: 'up' requires number of nodes as an argument"
            print
            sys.exit(1)

        cluster.up(args.num_nodes)
        update_ansible_inventory(cluster)
        print "Cluster has been started and resources provisioned."
        print "Next we'll use 'ansible' to install and configure software"

        # wait for a while for the last nodes to boot
        time.sleep(5)

        run_first_time_setup()

        print
        print "Cluster setup done."
        print
        print_usage_instructions(cluster)

    # bring cluster down
    elif command == 'down':
        print
        print "Shutting cluster down, starting with last nodes"
        print
        cluster.down()
        update_ansible_inventory(cluster)

    # run ansible configuration scripts on existing cluster
    elif command == 'configure':
        print "Configuring existing cluster with ansible"
        run_configuration()
        print_usage_instructions(cluster)

    # run hard reset on nodes
    elif command == 'reset_nodes':
        cluster.reset_nodes()

    # destroy all provisioned volumes
    elif command == 'destroy_volumes':
        cluster.destroy_volumes()

    # print info about provisioned resources
    elif command == 'info':
        print
        for line in cluster.get_info():
            print line

    else:
        raise RuntimeError("Unknown command '%s'" % command)

    # finally save provisioning actions to a log file for later reference
    prov_log = cluster.get_provisioning_log()
    if len(prov_log) > 0:
        # save provisioning log and look for changes
        with open('provisioning.log', 'a') as logfile:
            for le in prov_log:
                sep = ''
                for field in ['time', 'action', 'resource_type', 'resource_id', 'info']:
                    logfile.write('%s%s' % (sep, le[field]))
                    sep = '\t'
                logfile.write('\n')


if __name__ == '__main__':
    main()

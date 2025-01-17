# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
import os
from itertools import chain
import shutil
import subprocess

from charmhelpers.contrib.openstack.neutron import neutron_plugin_attribute
from copy import deepcopy

from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.openstack.utils import (
    pause_unit,
    resume_unit,
    make_assess_status_func,
    is_unit_paused_set,
    os_application_version_set,
    remote_restart,
    CompareOpenStackReleases,
    os_release,
)
from collections import OrderedDict
import neutron_ovs_context
from charmhelpers.contrib.network.ovs import (
    add_bridge,
    add_bridge_port,
    is_linuxbridge_interface,
    add_ovsbridge_linuxbridge,
    full_restart,
    enable_ipfix,
    disable_ipfix,
)
from charmhelpers.core.hookenv import (
    config,
    status_set,
    log,
    DEBUG,
)
from charmhelpers.contrib.openstack.neutron import (
    parse_bridge_mappings,
    determine_dkms_package,
    headers_package,
)
from charmhelpers.contrib.openstack.context import (
    ExternalPortContext,
    DataPortContext,
    WorkerConfigContext,
    parse_data_port_mappings,
)
from charmhelpers.core.host import (
    lsb_release,
    service,
    service_restart,
    service_running,
    CompareHostReleases,
    init_is_systemd,
    group_exists,
    user_exists,
    is_container,
)
from charmhelpers.core.kernel import (
    modprobe,
)

from charmhelpers.fetch import (
    apt_install,
    apt_purge,
    apt_update,
    filter_installed_packages,
    filter_missing_packages,
    apt_autoremove,
    get_upstream_version
)

from pci import PCINetDevices


# The interface is said to be satisfied if anyone of the interfaces in the
# list has a complete context.
# LY: Note the neutron-plugin is always present since that is the relation
#     with the principle and no data currently flows down from the principle
#     so there is no point in having it in REQUIRED_INTERFACES
REQUIRED_INTERFACES = {
    'messaging': ['amqp', 'zeromq-configuration'],
}

VERSION_PACKAGE = 'neutron-common'
NOVA_CONF_DIR = "/etc/nova"
NEUTRON_DHCP_AGENT_CONF = "/etc/neutron/dhcp_agent.ini"
NEUTRON_DNSMASQ_CONF = "/etc/neutron/dnsmasq.conf"
NEUTRON_CONF_DIR = "/etc/neutron"
NEUTRON_CONF = '%s/neutron.conf' % NEUTRON_CONF_DIR
NEUTRON_DEFAULT = '/etc/default/neutron-server'
NEUTRON_L3_AGENT_CONF = "/etc/neutron/l3_agent.ini"
NEUTRON_FWAAS_CONF = "/etc/neutron/fwaas_driver.ini"
ML2_CONF = '%s/plugins/ml2/ml2_conf.ini' % NEUTRON_CONF_DIR
OVS_CONF = '%s/plugins/ml2/openvswitch_agent.ini' % NEUTRON_CONF_DIR
EXT_PORT_CONF = '/etc/init/ext-port.conf'
NEUTRON_METADATA_AGENT_CONF = "/etc/neutron/metadata_agent.ini"
DVR_PACKAGES = [
    'neutron-l3-agent',
    'libnetfilter-log1',
]
DHCP_PACKAGES = ['neutron-dhcp-agent']
# haproxy is required for isolated provider networks
# ns-metadata-proxy LP#1831935
METADATA_PACKAGES = ['neutron-metadata-agent', 'haproxy']
# conntrack is a dependency of neutron-l3-agent and hence is not added
L3HA_PACKAGES = ['keepalived']

PY3_PACKAGES = [
    'python3-neutron',
    'python3-zmq',  # fwaas_v2_log
]

PURGE_PACKAGES = [
    'python-neutron',
    'python-neutron-fwaas',
]

PHY_NIC_MTU_CONF = '/etc/init/os-charm-phy-nic-mtu.conf'
TEMPLATES = 'templates/'
OVS_DEFAULT = '/etc/default/openvswitch-switch'
DPDK_INTERFACES = '/etc/dpdk/interfaces'
NEUTRON_SRIOV_AGENT_CONF = os.path.join(NEUTRON_CONF_DIR,
                                        'plugins/ml2/sriov_agent.ini')
NEUTRON_SRIOV_INIT_SCRIPT = os.path.join('/usr/local/bin',
                                         'neutron-openvswitch-'
                                         'networking-sriov.sh')
NEUTRON_SRIOV_INIT_PY_SCRIPT = os.path.join('/usr/local/bin',
                                            'neutron_openvswitch_'
                                            'networking_sriov.py')
NEUTRON_SRIOV_INIT_DEFAULT = os.path.join('/etc/default',
                                          'neutron-openvswitch-'
                                          'networking-sriov')
NEUTRON_SRIOV_SYSTEMD_UNIT = os.path.join('/lib/systemd/system',
                                          'neutron-openvswitch-'
                                          'networking-sriov.service')
NEUTRON_SRIOV_UPSTART_CONF = os.path.join('/etc/init',
                                          'neutron-openvswitch-'
                                          'networking-sriov.conf')

BASE_RESOURCE_MAP = OrderedDict([
    (NEUTRON_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext(),
                     neutron_ovs_context.RemoteRestartContext(
                         ['neutron-plugin', 'neutron-control']),
                     context.AMQPContext(ssl_dir=NEUTRON_CONF_DIR),
                     context.ZeroMQContext(),
                     context.NotificationDriverContext(),
                     neutron_ovs_context.HostIPContext(),
                     neutron_ovs_context.ZoneContext(),
                     ],
    }),
    (ML2_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_CONF, {
        'services': ['neutron-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_DEFAULT, {
        'services': ['openvswitch-switch'],
        'contexts': [neutron_ovs_context.OVSDPDKDeviceContext(),
                     neutron_ovs_context.RemoteRestartContext(
                         ['neutron-plugin', 'neutron-control'])],
    }),
    (DPDK_INTERFACES, {
        'services': ['dpdk', 'openvswitch-switch'],
        'contexts': [neutron_ovs_context.DPDKDeviceContext()],
    }),
    (PHY_NIC_MTU_CONF, {
        'services': ['os-charm-phy-nic-mtu'],
        'contexts': [context.PhyNICMTUContext()],
    }),
])
METADATA_RESOURCE_MAP = OrderedDict([
    (NEUTRON_METADATA_AGENT_CONF, {
        'services': ['neutron-metadata-agent'],
        'contexts': [neutron_ovs_context.SharedSecretContext(),
                     neutron_ovs_context.APIIdentityServiceContext(),
                     WorkerConfigContext()],
    }),
])
DHCP_RESOURCE_MAP = OrderedDict([
    (NEUTRON_DHCP_AGENT_CONF, {
        'services': ['neutron-dhcp-agent'],
        'contexts': [neutron_ovs_context.DHCPAgentContext()],
    }),
    (NEUTRON_DNSMASQ_CONF, {
        'services': ['neutron-dhcp-agent'],
        'contexts': [neutron_ovs_context.DHCPAgentContext()],
    }),
])
DVR_RESOURCE_MAP = OrderedDict([
    (NEUTRON_L3_AGENT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (NEUTRON_FWAAS_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (EXT_PORT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [context.ExternalPortContext()],
    }),
])
SRIOV_RESOURCE_MAP = OrderedDict([
    (NEUTRON_SRIOV_AGENT_CONF, {
        'services': ['neutron-sriov-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (NEUTRON_SRIOV_INIT_DEFAULT, {
        'services': [],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (NEUTRON_SRIOV_UPSTART_CONF, {
        'services': [],
        'contexts': [],
    }),
])

TEMPLATES = 'templates/'
INT_BRIDGE = "br-int"
EXT_BRIDGE = "br-ex"
DATA_BRIDGE = 'br-data'


def install_packages():
    apt_update()
    # NOTE(jamespage): install neutron-common package so we always
    #                  get a clear signal on which OS release is
    #                  being deployed
    apt_install(filter_installed_packages(['neutron-common']),
                fatal=True)
    # NOTE(jamespage): ensure early install of dkms related
    #                  dependencies for kernels which need
    #                  openvswitch via dkms (12.04).
    dkms_packages = determine_dkms_package()
    if dkms_packages:
        apt_install([headers_package()] + dkms_packages, fatal=True)
    missing_packages = filter_installed_packages(determine_packages())
    if missing_packages:
        status_set('maintenance', 'Installing packages')
        apt_install(missing_packages,
                    fatal=True)
    if use_dpdk():
        enable_ovs_dpdk()

    # NOTE(tpsilva): if we're using openvswitch driver, we need to explicitly
    #                load the nf_conntrack_ipv4/6 module, since it won't be
    #                loaded automatically in some cases. LP#1834213
    if not is_container() and config('firewall-driver') == 'openvswitch':
        try:
            modprobe('nf_conntrack_ipv4', True)
            modprobe('nf_conntrack_ipv6', True)
        except subprocess.CalledProcessError:
            # Newer kernel versions (4.19+) don't have two modules for that, so
            # only load nf_conntrack
            log("This kernel does not have nf_conntrack_ipv4/6. "
                "Loading nf_conntrack only.")
            modprobe('nf_conntrack', True)


def install_l3ha_packages():
    apt_update()
    apt_install(L3HA_PACKAGES, fatal=True)


def purge_packages(pkg_list):
    purge_pkgs = []
    required_packages = determine_packages()
    for pkg in pkg_list:
        if pkg not in required_packages:
            purge_pkgs.append(pkg)
    purge_pkgs = filter_missing_packages(purge_pkgs)
    if purge_pkgs:
        status_set('maintenance', 'Purging unused packages')
        apt_purge(purge_pkgs, fatal=True)
        apt_autoremove(purge=True, fatal=True)


def determine_packages():
    pkgs = []
    py3_pkgs = []
    plugin_pkgs = neutron_plugin_attribute('ovs', 'packages', 'neutron')
    for plugin_pkg in plugin_pkgs:
        pkgs.extend(plugin_pkg)
    if use_dvr():
        pkgs.extend(DVR_PACKAGES)
        py3_pkgs.append('python3-neutron-fwaas')
        _os_release = os_release('neutron-common', base='icehouse')
        # per 17.08 release notes L3HA + DVR is a Newton+ feature
        if (use_l3ha() and
                CompareOpenStackReleases(_os_release) >= 'newton'):
            pkgs.extend(L3HA_PACKAGES)
    if enable_local_dhcp():
        pkgs.extend(DHCP_PACKAGES)
        pkgs.extend(METADATA_PACKAGES)

    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse',
                   reset_cache=True))
    if cmp_release >= 'mitaka' and 'neutron-plugin-openvswitch-agent' in pkgs:
        pkgs.remove('neutron-plugin-openvswitch-agent')
        pkgs.append('neutron-openvswitch-agent')

    if use_dpdk():
        pkgs.append('openvswitch-switch-dpdk')

    if enable_sriov():
        if cmp_release >= 'mitaka':
            pkgs.append('neutron-sriov-agent')
        else:
            pkgs.append('neutron-plugin-sriov-agent')

    if cmp_release >= 'rocky':
        pkgs = [p for p in pkgs if not p.startswith('python-')]
        pkgs.extend(PY3_PACKAGES)
        pkgs.extend(py3_pkgs)

    return pkgs


def determine_purge_packages():
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse',
                   reset_cache=True))
    if cmp_release >= 'rocky':
        return PURGE_PACKAGES
    return []


def register_configs(release=None):
    release = release or os_release('neutron-common', base='icehouse')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().items():
        configs.register(cfg, rscs['contexts'])
    return configs


def resource_map():
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    drop_config = []
    resource_map = deepcopy(BASE_RESOURCE_MAP)
    if use_dvr():
        resource_map.update(DVR_RESOURCE_MAP)
        resource_map.update(METADATA_RESOURCE_MAP)
        dvr_services = ['neutron-metadata-agent', 'neutron-l3-agent']
        resource_map[NEUTRON_CONF]['services'] += dvr_services
    if enable_local_dhcp():
        resource_map.update(METADATA_RESOURCE_MAP)
        resource_map.update(DHCP_RESOURCE_MAP)
        metadata_services = ['neutron-metadata-agent', 'neutron-dhcp-agent']
        resource_map[NEUTRON_CONF]['services'] += metadata_services
    # Remap any service names as required
    _os_release = os_release('neutron-common', base='icehouse')
    if CompareOpenStackReleases(_os_release) >= 'mitaka':
        # ml2_conf.ini -> openvswitch_agent.ini
        drop_config.append(ML2_CONF)
        # drop of -plugin from service name
        resource_map[NEUTRON_CONF]['services'].remove(
            'neutron-plugin-openvswitch-agent'
        )
        resource_map[NEUTRON_CONF]['services'].append(
            'neutron-openvswitch-agent'
        )
        if not use_dpdk():
            drop_config.append(DPDK_INTERFACES)
            drop_config.append(OVS_DEFAULT)
        elif ovs_has_late_dpdk_init():
            drop_config.append(OVS_DEFAULT)
    else:
        drop_config.extend([OVS_CONF, DPDK_INTERFACES])

    if enable_sriov():
        sriov_agent_name = 'neutron-sriov-agent'
        sriov_resource_map = deepcopy(SRIOV_RESOURCE_MAP)

        if CompareOpenStackReleases(_os_release) < 'mitaka':
            sriov_agent_name = 'neutron-plugin-sriov-agent'
            # Patch resource_map for Kilo and Liberty
            sriov_resource_map[NEUTRON_SRIOV_AGENT_CONF]['services'] = \
                [sriov_agent_name]

        resource_map.update(sriov_resource_map)
        resource_map[NEUTRON_CONF]['services'].append(
            sriov_agent_name)

    # Use MAAS1.9 for MTU and external port config on xenial and above
    if CompareHostReleases(lsb_release()['DISTRIB_CODENAME']) >= 'xenial':
        drop_config.extend([EXT_PORT_CONF, PHY_NIC_MTU_CONF])

    for _conf in drop_config:
        try:
            del resource_map[_conf]
        except KeyError:
            pass

    return resource_map


def restart_map():
    '''
    Constructs a restart map based on charm config settings and relation
    state.
    '''
    return {k: v['services'] for k, v in resource_map().items()}


def services():
    """Returns a list of (unique) services associate with this charm
    Note that we drop the os-charm-phy-nic-mtu service as it's not an actual
    running service that we can check for.

    @returns [strings] - list of service names suitable for (re)start_service()
    """
    s_set = set(chain(*restart_map().values()))
    s_set.discard('os-charm-phy-nic-mtu')
    return list(s_set)


def determine_ports():
    """Assemble a list of API ports for services the charm is managing

    @returns [ports] - list of ports that the charm manages.
    """
    ports = []
    if use_dvr():
        ports.append(DVR_RESOURCE_MAP[EXT_PORT_CONF]["ext_port"])
    return ports


UPDATE_ALTERNATIVES = ['update-alternatives', '--set', 'ovs-vswitchd']
OVS_DPDK_BIN = '/usr/lib/openvswitch-switch-dpdk/ovs-vswitchd-dpdk'
OVS_DEFAULT_BIN = '/usr/lib/openvswitch-switch/ovs-vswitchd'


# TODO(jamespage): rework back to charmhelpers
def set_Open_vSwitch_column_value(column, value):
    """
    Calls ovs-vsctl and sets the 'column=value' in the Open_vSwitch table.

    :param column: colume name to set value for
    :param value: value to set
            See http://www.openvswitch.org//ovs-vswitchd.conf.db.5.pdf for
            details of the relevant values.
    :type str
    :returns bool: indicating if a column value was changed
    :raises CalledProcessException: possibly ovsdb-server is not running
    """
    current_value = None
    try:
        current_value = json.loads(subprocess.check_output(
            ['ovs-vsctl', 'get', 'Open_vSwitch', '.', column]
        ))
    except subprocess.CalledProcessError:
        pass

    if current_value != value:
        log('Setting {}:{} in the Open_vSwitch table'.format(column, value))
        subprocess.check_call(['ovs-vsctl', 'set', 'Open_vSwitch',
                               '.', '{}={}'.format(column,
                                                   value)])
        return True
    return False


def enable_ovs_dpdk():
    '''Enables the DPDK variant of ovs-vswitchd and restarts it'''
    subprocess.check_call(UPDATE_ALTERNATIVES + [OVS_DPDK_BIN])
    values_changed = []
    if ovs_has_late_dpdk_init():
        dpdk_context = neutron_ovs_context.OVSDPDKDeviceContext()
        other_config = OrderedDict([
            ('dpdk-lcore-mask', dpdk_context.cpu_mask()),
            ('dpdk-socket-mem', dpdk_context.socket_memory()),
            ('dpdk-init', 'true'),
        ])
        if not ovs_vhostuser_client():
            other_config['dpdk-extra'] = (
                '--vhost-owner libvirt-qemu:kvm --vhost-perm 0660 ' +
                dpdk_context.pci_whitelist()
            )
        else:
            other_config['dpdk-extra'] = (
                dpdk_context.pci_whitelist()
            )
        other_config['dpdk-init'] = 'true'
        for column, value in other_config.items():
            values_changed.append(
                set_Open_vSwitch_column_value(
                    'other_config:{}'.format(column),
                    value
                )
            )
    if ((values_changed and any(values_changed)) and
            not is_unit_paused_set()):
        service_restart('openvswitch-switch')


def install_tmpfilesd():
    '''Install systemd-tmpfiles configuration for ovs vhost-user sockets'''
    # NOTE(jamespage): Only do this if libvirt is actually installed
    if (init_is_systemd() and
            user_exists('libvirt-qemu') and
            group_exists('kvm')):
        shutil.copy('files/nova-ovs-vhost-user.conf',
                    '/etc/tmpfiles.d')
        subprocess.check_call(['systemd-tmpfiles', '--create'])


def install_sriov_systemd_files():
    '''Install SR-IOV systemd files'''
    shutil.copy('files/neutron_openvswitch_networking_sriov.py',
                '/usr/local/bin')
    shutil.copy('files/neutron-openvswitch-networking-sriov.sh',
                '/usr/local/bin')
    shutil.copy('files/neutron-openvswitch-networking-sriov.service',
                '/lib/systemd/system')


def configure_ovs():
    status_set('maintenance', 'Configuring ovs')
    if not service_running('openvswitch-switch'):
        full_restart()
    datapath_type = determine_datapath_type()
    add_bridge(INT_BRIDGE, datapath_type)
    add_bridge(EXT_BRIDGE, datapath_type)
    ext_port_ctx = None
    if use_dvr():
        ext_port_ctx = ExternalPortContext()()
    if ext_port_ctx and ext_port_ctx['ext_port']:
        add_bridge_port(EXT_BRIDGE, ext_port_ctx['ext_port'])

    modern_ovs = ovs_has_late_dpdk_init()

    bridgemaps = None
    if not use_dpdk():
        portmaps = DataPortContext()()
        bridgemaps = parse_bridge_mappings(config('bridge-mappings'))
        for br in bridgemaps.values():
            add_bridge(br, datapath_type)
            if not portmaps:
                continue

            for port, _br in portmaps.items():
                if _br == br:
                    if not is_linuxbridge_interface(port):
                        add_bridge_port(br, port, promisc=True)
                    else:
                        add_ovsbridge_linuxbridge(br, port)
    else:
        log('Configuring bridges with DPDK', level=DEBUG)
        global_mtu = (
            neutron_ovs_context.NeutronAPIContext()()['global_physnet_mtu'])
        # NOTE: when in dpdk mode, add based on pci bus order
        #       with type 'dpdk'
        bridgemaps = neutron_ovs_context.resolve_dpdk_bridges()
        log('bridgemaps: {}'.format(bridgemaps), level=DEBUG)
        device_index = 0
        for pci_address, br in bridgemaps.items():
            log('Adding DPDK bridge: {}:{}'.format(br, datapath_type),
                level=DEBUG)
            add_bridge(br, datapath_type)
            if modern_ovs:
                portname = 'dpdk-{}'.format(
                    hashlib.sha1(pci_address.encode('UTF-8')).hexdigest()[:7]
                )
            else:
                portname = 'dpdk{}'.format(device_index)

            log('Adding DPDK port: {}:{}:{}'.format(br, portname,
                                                    pci_address),
                level=DEBUG)
            dpdk_add_bridge_port(br, portname,
                                 pci_address)
            # TODO(sahid): We should also take into account the
            # "physical-network-mtus" in case different MTUs are
            # configured based on physical networks.
            dpdk_set_mtu_request(portname, global_mtu)
            device_index += 1

        if modern_ovs:
            log('Configuring bridges with modern_ovs/DPDK',
                level=DEBUG)
            bondmaps = neutron_ovs_context.resolve_dpdk_bonds()
            log('bondmaps: {}'.format(bondmaps), level=DEBUG)
            bridge_bond_map = DPDKBridgeBondMap()
            portmap = parse_data_port_mappings(config('data-port'))
            log('portmap: {}'.format(portmap), level=DEBUG)
            for pci_address, bond in bondmaps.items():
                if bond in portmap:
                    log('Adding DPDK bridge: {}:{}'.format(portmap[bond],
                                                           datapath_type),
                        level=DEBUG)
                    add_bridge(portmap[bond], datapath_type)
                    portname = 'dpdk-{}'.format(
                        hashlib.sha1(pci_address.encode('UTF-8'))
                        .hexdigest()[:7]
                    )
                    bridge_bond_map.add_port(portmap[bond], bond,
                                             portname, pci_address)

            log('bridge_bond_map: {}'.format(bridge_bond_map),
                level=DEBUG)
            bond_configs = DPDKBondsConfig()
            for br, bonds in bridge_bond_map.items():
                for bond, port_map in bonds.items():
                    log('Adding DPDK bond: {}:{}:{}'.format(br, bond,
                                                            port_map),
                        level=DEBUG)
                    dpdk_add_bridge_bond(br, bond, port_map)
                    dpdk_set_interfaces_mtu(
                        global_mtu,
                        port_map.keys())
                    log('Configuring DPDK bond: {}:{}'.format(
                        bond,
                        bond_configs.get_bond_config(bond)),
                        level=DEBUG)
                    dpdk_set_bond_config(
                        bond,
                        bond_configs.get_bond_config(bond)
                    )

    target = config('ipfix-target')
    bridges = [INT_BRIDGE, EXT_BRIDGE]
    if bridgemaps:
        bridges.extend(bridgemaps.values())

    if target:
        for bridge in bridges:
            disable_ipfix(bridge)
            enable_ipfix(bridge, target)
    else:
        # NOTE: removing ipfix setting from a bridge is idempotent and
        #       will pass regardless of the existence of the setting
        for bridge in bridges:
            disable_ipfix(bridge)

    # Ensure this runs so that mtu is applied to data-port interfaces if
    # provided.
    # NOTE(ajkavanagh) for pause/resume we don't gate this as it's not a
    # running service, but rather running a few commands.
    service_restart('os-charm-phy-nic-mtu')


def _get_interfaces_from_mappings(sriov_mappings):
    """Returns list of interfaces based on sriov-device-mappings"""
    interfaces = []
    if sriov_mappings:
        # <net>:<interface>[ <net>:<interface>] configuration
        for token in sriov_mappings.split():
            _, interface = token.split(':')
            interfaces.append(interface)
    return interfaces


def configure_sriov():
    '''Configure SR-IOV devices based on provided configuration options

    NOTE(fnordahl): Boot time configuration is done by init script
    intalled by this charm.

    This function only does runtime configuration!
    '''
    charm_config = config()
    if not enable_sriov():
        return

    install_sriov_systemd_files()
    # make sure that boot time execution is enabled
    service('enable', 'neutron-openvswitch-networking-sriov')

    devices = PCINetDevices()
    sriov_numvfs = charm_config.get('sriov-numvfs')

    # automatic configuration of all SR-IOV devices
    if sriov_numvfs == 'auto':
        interfaces = _get_interfaces_from_mappings(
            charm_config.get('sriov-device-mappings'))
        log('Configuring SR-IOV device VF functions in auto mode')
        for device in devices.pci_devices:
            if device and device.sriov:
                if interfaces and device.interface_name not in interfaces:
                    log("Excluding configuration of SR-IOV device {}.".format(
                        device.interface_name))
                else:
                    log("Configuring SR-IOV device"
                        " {} with {} VF's".format(device.interface_name,
                                                  device.sriov_totalvfs))
                    device.set_sriov_numvfs(device.sriov_totalvfs)
    else:
        # Single int blanket configuration
        try:
            log('Configuring SR-IOV device VF functions'
                ' with blanket setting')
            for device in devices.pci_devices:
                if device and device.sriov:
                    numvfs = min(int(sriov_numvfs), device.sriov_totalvfs)
                    if int(sriov_numvfs) > device.sriov_totalvfs:
                        log('Requested value for sriov-numvfs ({}) too '
                            'high for interface {}. Falling back to '
                            'interface totalvfs '
                            'value: {}'.format(sriov_numvfs,
                                               device.interface_name,
                                               device.sriov_totalvfs))
                    log("Configuring SR-IOV device {} with {} "
                        "VFs".format(device.interface_name, numvfs))
                    device.set_sriov_numvfs(numvfs)
        except ValueError:
            # <device>:<numvfs>[ <device>:numvfs] configuration
            sriov_numvfs = sriov_numvfs.split()
            for device_config in sriov_numvfs:
                log('Configuring SR-IOV device VF functions per interface')
                interface_name, numvfs = device_config.split(':')
                device = devices.get_device_from_interface_name(
                    interface_name)
                if device and device.sriov:
                    if int(numvfs) > device.sriov_totalvfs:
                        log('Requested value for sriov-numfs ({}) too '
                            'high for interface {}. Falling back to '
                            'interface totalvfs '
                            'value: {}'.format(numvfs,
                                               device.interface_name,
                                               device.sriov_totalvfs))
                        numvfs = device.sriov_totalvfs
                    log("Configuring SR-IOV device {} with {} "
                        "VF's".format(device.interface_name, numvfs))
                    device.set_sriov_numvfs(int(numvfs))

    # Trigger remote restart in parent application
    remote_restart('neutron-plugin', 'nova-compute')

    # Restart of SRIOV agent is required after changes to system runtime
    # VF configuration
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    if cmp_release >= 'mitaka':
        service_restart('neutron-sriov-agent')
    else:
        service_restart('neutron-plugin-sriov-agent')


def get_shared_secret():
    ctxt = neutron_ovs_context.SharedSecretContext()()
    if 'shared_secret' in ctxt:
        return ctxt['shared_secret']


def use_dvr():
    return context.NeutronAPIContext()().get('enable_dvr', False)


def use_l3ha():
    return context.NeutronAPIContext()().get('enable_l3ha', False)


def determine_datapath_type():
    '''
    Determine the ovs datapath type to use

    @returns string containing the datapath type
    '''
    if use_dpdk():
        return 'netdev'
    return 'system'


def use_dpdk():
    '''Determine whether DPDK should be used'''
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    return (cmp_release >= 'mitaka' and config('enable-dpdk'))


def ovs_has_late_dpdk_init():
    ''' OVS 2.6.0 introduces late initialization '''
    import apt_pkg
    ovs_version = get_upstream_version("openvswitch-switch")
    return apt_pkg.version_compare(ovs_version, '2.6.0') >= 0


def ovs_vhostuser_client():
    '''
    Determine whether OVS will act as a client on the vhostuser socket

    @returns boolean indicating whether OVS will act as a client
    '''
    import apt_pkg
    ovs_version = get_upstream_version("openvswitch-switch")
    return apt_pkg.version_compare(ovs_version, '2.9.0') >= 0


def enable_sriov():
    '''Determine whether SR-IOV is enabled and supported'''
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    return (cmp_release >= 'kilo' and config('enable-sriov'))


# TODO: update into charm-helpers to add port_type parameter
def dpdk_add_bridge_port(name, port, pci_address=None):
    ''' Add a port to the named openvswitch bridge '''
    # log('Adding port {} to bridge {}'.format(port, name))
    if ovs_has_late_dpdk_init():
        cmd = ["ovs-vsctl",
               "add-port", name, port, "--",
               "set", "Interface", port,
               "type=dpdk",
               "options:dpdk-devargs={}".format(pci_address)]
    else:
        cmd = ["ovs-vsctl", "--",
               "--may-exist", "add-port", name, port,
               "--", "set", "Interface", port, "type=dpdk"]
    subprocess.check_call(cmd)


def dpdk_add_bridge_bond(bridge_name, bond_name, port_map):
    ''' Add ports to a bond attached to the named openvswitch bridge '''
    if not ovs_has_late_dpdk_init():
        raise Exception("Bonds are not supported for OVS pre-2.6.0")

    cmd = ["ovs-vsctl", "--may-exist",
           "add-bond", bridge_name, bond_name]
    for portname in port_map.keys():
        cmd.append(portname)
    for portname, pci_address in port_map.items():
        cmd.extend(["--", "set", "Interface", portname,
                    "type=dpdk",
                    "options:dpdk-devargs={}".format(pci_address)])
    subprocess.check_call(cmd)


def dpdk_set_bond_config(bond_name, config):
    if not ovs_has_late_dpdk_init():
        raise Exception("Bonds are not supported for OVS pre-2.6.0")

    cmd = ["ovs-vsctl",
           "--", "set", "port", bond_name,
           "bond_mode={}".format(config['mode']),
           "--", "set", "port", bond_name,
           "lacp={}".format(config['lacp']),
           "--", "set", "port", bond_name,
           "other_config:lacp-time=={}".format(config['lacp-time']),
           ]
    subprocess.check_call(cmd)


def dpdk_set_mtu_request(port, mtu):
    cmd = ["ovs-vsctl", "set", "Interface", port,
           "mtu_request={}".format(mtu)]
    subprocess.check_call(cmd)


def dpdk_set_interfaces_mtu(mtu, ports):
    """Set MTU on dpdk ports.

    :param mtu: Name of unit to match
    :type mtu: str
    :param ports: List of ports
    :type ports: []
    """
    for port in ports:
        dpdk_set_mtu_request(port, mtu)


def enable_nova_metadata():
    return use_dvr() or enable_local_dhcp()


def enable_local_dhcp():
    return config('enable-local-dhcp-and-metadata')


def assess_status(configs):
    """Assess status of current unit
    Decides what the state of the unit should be based on the current
    configuration.
    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()
    os_application_version_set(VERSION_PACKAGE)


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    Note that required_interfaces is augmented with neutron-plugin-api if the
    nova_metadata is enabled.

    NOTE(ajkavanagh) ports are not checked due to race hazards with services
    that don't behave sychronously w.r.t their service scripts.  e.g.
    apache2.
    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    required_interfaces = REQUIRED_INTERFACES.copy()
    if enable_nova_metadata():
        required_interfaces['neutron-plugin-api'] = ['neutron-plugin-api']
    return make_assess_status_func(
        configs, required_interfaces,
        services=services(), ports=None)


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit
    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    f(assess_status_func(configs),
      services=services(),
      ports=None)


class DPDKBridgeBondMap():

    def __init__(self):
        self.map = {}

    def add_port(self, bridge, bond, portname, pci_address):
        if bridge not in self.map:
            self.map[bridge] = {}
        if bond not in self.map[bridge]:
            self.map[bridge][bond] = {}
        self.map[bridge][bond][portname] = pci_address

    def items(self):
        return list(self.map.items())


class DPDKBondsConfig():
    '''
    A class to parse dpdk-bond-config into a dictionary and
    provide a convenient config get interface.
    '''

    DEFAUL_LACP_CONFIG = {
        'mode': 'balance-tcp',
        'lacp': 'active',
        'lacp-time': 'fast'
    }
    ALL_BONDS = 'ALL_BONDS'

    BOND_MODES = ['active-backup', 'balance-slb', 'balance-tcp']
    BOND_LACP = ['active', 'passive', 'off']
    BOND_LACP_TIME = ['fast', 'slow']

    def __init__(self):

        self.lacp_config = {
            self.ALL_BONDS: deepcopy(self.DEFAUL_LACP_CONFIG)
        }

        lacp_config = config('dpdk-bond-config')
        if lacp_config:
            lacp_config_map = lacp_config.split()
            for entry in lacp_config_map:
                bond, entry = self._partition_entry(entry)
                if not bond:
                    bond = self.ALL_BONDS

                mode, entry = self._partition_entry(entry)
                if not mode:
                    mode = self.DEFAUL_LACP_CONFIG['mode']
                assert mode in self.BOND_MODES, \
                    "Bond mode {} is invalid".format(mode)

                lacp, entry = self._partition_entry(entry)
                if not lacp:
                    lacp = self.DEFAUL_LACP_CONFIG['lacp']
                assert lacp in self.BOND_LACP, \
                    "Bond lacp {} is invalid".format(lacp)

                lacp_time, entry = self._partition_entry(entry)
                if not lacp_time:
                    lacp_time = self.DEFAUL_LACP_CONFIG['lacp-time']
                assert lacp_time in self.BOND_LACP_TIME, \
                    "Bond lacp-time {} is invalid".format(lacp_time)

                self.lacp_config[bond] = {
                    'mode': mode,
                    'lacp': lacp,
                    'lacp-time': lacp_time
                }

    def _partition_entry(self, entry):
        t = entry.partition(":")
        return t[0], t[2]

    def get_bond_config(self, bond):
        '''
        Get the LACP configuration for a bond

        :param bond: the bond name
        :return: a dictionary with the configuration of the
        '''
        if bond not in self.lacp_config:
            return self.lacp_config[self.ALL_BONDS]

        return self.lacp_config[bond]

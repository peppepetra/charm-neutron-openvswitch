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
import subprocess

from mock import MagicMock, patch, call
from collections import OrderedDict
import charmhelpers.contrib.openstack.templating as templating

templating.OSConfigRenderer = MagicMock()

import neutron_ovs_utils as nutils
import neutron_ovs_context

from test_utils import (
    CharmTestCase,
)
import charmhelpers
import charmhelpers.core.hookenv as hookenv


TO_PATCH = [
    'add_bridge',
    'add_bridge_port',
    'add_ovsbridge_linuxbridge',
    'is_linuxbridge_interface',
    'dpdk_add_bridge_port',
    'dpdk_add_bridge_bond',
    'dpdk_set_bond_config',
    'dpdk_set_mtu_request',
    'dpdk_set_interfaces_mtu',
    'apt_install',
    'apt_update',
    'config',
    'os_release',
    'filter_installed_packages',
    'filter_missing_packages',
    'lsb_release',
    'neutron_plugin_attribute',
    'full_restart',
    'service',
    'service_restart',
    'service_running',
    'ExternalPortContext',
    'determine_dkms_package',
    'headers_package',
    'status_set',
    'use_dpdk',
    'os_application_version_set',
    'remote_restart',
    'PCINetDevices',
    'enable_ipfix',
    'disable_ipfix',
    'ovs_has_late_dpdk_init',
    'ovs_vhostuser_client',
    'parse_data_port_mappings',
    'user_exists',
    'group_exists',
    'init_is_systemd',
    'modprobe',
    'is_container',
]

head_pkg = 'linux-headers-3.15.0-5-generic'


def _mock_npa(plugin, attr, net_manager=None):
    plugins = {
        'ovs': {
            'config': '/etc/neutron/plugins/ml2/ml2_conf.ini',
            'driver': 'neutron.plugins.ml2.plugin.Ml2Plugin',
            'contexts': [],
            'services': ['neutron-plugin-openvswitch-agent'],
            'packages': [[head_pkg], ['neutron-plugin-openvswitch-agent']],
            'server_packages': ['neutron-server',
                                'neutron-plugin-ml2'],
            'server_services': ['neutron-server']
        },
    }
    return plugins[plugin][attr]


class DummyContext():

    def __init__(self, return_value):
        self.return_value = return_value

    def __call__(self):
        return self.return_value


class TestNeutronOVSUtils(CharmTestCase):

    def setUp(self):
        super(TestNeutronOVSUtils, self).setUp(nutils, TO_PATCH)
        self.neutron_plugin_attribute.side_effect = _mock_npa
        self.config.side_effect = self.test_config.get
        self.use_dpdk.return_value = False
        self.ovs_has_late_dpdk_init.return_value = False
        self.ovs_vhostuser_client.return_value = False

    def tearDown(self):
        # Reset cached cache
        hookenv.cache = {}

    @patch.object(nutils, 'determine_packages')
    def test_install_packages(self, _determine_packages):
        self.os_release.return_value = 'mitaka'
        _determine_packages.return_value = 'randompkg'
        nutils.install_packages()
        self.apt_update.assert_called_with()
        self.apt_install.assert_called_with(self.filter_installed_packages(),
                                            fatal=True)
        self.modprobe.assert_not_called()

    @patch.object(nutils, 'determine_packages')
    def test_install_packages_container(self, _determine_packages):
        self.os_release.return_value = 'mitaka'
        self.is_container.return_value = True
        _determine_packages.return_value = 'randompkg'
        nutils.install_packages()
        self.apt_update.assert_called_with()
        self.apt_install.assert_called_with(self.filter_installed_packages(),
                                            fatal=True)
        self.modprobe.assert_not_called()

    @patch.object(nutils, 'determine_packages')
    def test_install_packages_ovs_firewall(self, _determine_packages):
        self.os_release.return_value = 'mitaka'
        _determine_packages.return_value = 'randompkg'
        self.is_container.return_value = False
        self.test_config.set('firewall-driver', 'openvswitch')
        nutils.install_packages()
        self.apt_update.assert_called_with()
        self.apt_install.assert_called_with(self.filter_installed_packages(),
                                            fatal=True)
        self.modprobe.assert_has_calls([call('nf_conntrack_ipv4', True),
                                        call('nf_conntrack_ipv6', True)])

    @patch.object(nutils, 'determine_packages')
    def test_install_packages_ovs_fw_newer_kernel(self, _determine_packages):
        self.os_release.return_value = 'mitaka'
        _determine_packages.return_value = 'randompkg'
        self.is_container.return_value = False
        self.test_config.set('firewall-driver', 'openvswitch')
        self.modprobe.side_effect = [subprocess.CalledProcessError(0, ""),
                                     None]
        nutils.install_packages()
        self.apt_update.assert_called_with()
        self.apt_install.assert_called_with(self.filter_installed_packages(),
                                            fatal=True)
        self.modprobe.assert_has_calls([call('nf_conntrack_ipv4', True),
                                        call('nf_conntrack', True)])

    @patch.object(nutils, 'determine_packages')
    def test_install_packages_dkms_needed(self, _determine_packages):
        self.os_release.return_value = 'mitaka'
        _determine_packages.return_value = 'randompkg'
        self.determine_dkms_package.return_value = \
            ['openvswitch-datapath-dkms']
        self.headers_package.return_value = 'linux-headers-foobar'
        nutils.install_packages()
        self.apt_update.assert_called_with()
        self.apt_install.assert_has_calls([
            call(['linux-headers-foobar',
                  'openvswitch-datapath-dkms'], fatal=True),
            call(self.filter_installed_packages(), fatal=True),
        ])

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages(self, _head_pkgs, _os_rel,
                                _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        _use_dvr.return_value = False
        _use_l3ha.return_value = False
        _os_rel.return_value = 'icehouse'
        self.os_release.return_value = 'icehouse'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-plugin-openvswitch-agent'
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_mitaka(self, _head_pkgs, _os_rel,
                                       _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        _use_dvr.return_value = False
        _use_l3ha.return_value = False
        _os_rel.return_value = 'mitaka'
        self.os_release.return_value = 'mitaka'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-openvswitch-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_metadata(self, _head_pkgs, _os_rel,
                                         _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', True)
        _use_dvr.return_value = False
        _use_l3ha.return_value = False
        _os_rel.return_value = 'icehouse'
        self.os_release.return_value = 'icehouse'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-plugin-openvswitch-agent',
            'neutron-dhcp-agent',
            'neutron-metadata-agent',
            'haproxy',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_dvr(self, _head_pkgs, _os_rel, _use_dvr,
                                    _use_l3ha):
        _use_dvr.return_value = True
        _use_l3ha.return_value = False
        _os_rel.return_value = 'icehouse'
        self.os_release.return_value = 'icehouse'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-plugin-openvswitch-agent',
            'neutron-l3-agent',
            'libnetfilter-log1',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_dvr_rocky(self, _head_pkgs, _os_rel, _use_dvr,
                                          _use_l3ha):
        _use_dvr.return_value = True
        _use_l3ha.return_value = False
        _os_rel.return_value = 'rocky'
        self.os_release.return_value = 'rocky'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-l3-agent',
            'libnetfilter-log1',
            'neutron-openvswitch-agent',
            'python3-neutron',
            'python3-zmq',
            'python3-neutron-fwaas',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_newton_dvr_l3ha(self, _head_pkgs, _os_rel,
                                                _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        _use_dvr.return_value = True
        _use_l3ha.return_value = True
        _os_rel.return_value = 'newton'
        self.os_release.return_value = 'newton'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-l3-agent',
            'libnetfilter-log1',
            'keepalived',
            'neutron-openvswitch-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_newton_dvr_no_l3ha(self, _head_pkgs, _os_rel,
                                                   _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        _use_dvr.return_value = True
        _use_l3ha.return_value = False
        _os_rel.return_value = 'newton'
        self.os_release.return_value = 'newton'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-l3-agent',
            'libnetfilter-log1',
            'neutron-openvswitch-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_packages_mitaka_dvr_l3ha(self, _head_pkgs, _os_rel,
                                                _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        _use_dvr.return_value = True
        _use_l3ha.return_value = True
        _os_rel.return_value = 'mitaka'
        self.os_release.return_value = 'mitaka'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-l3-agent',
            'libnetfilter-log1',
            'neutron-openvswitch-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_pkgs_sriov(self, _head_pkgs, _os_rel,
                                  _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        self.test_config.set('enable-sriov', True)
        _use_dvr.return_value = False
        _use_l3ha.return_value = False
        _os_rel.return_value = 'kilo'
        self.os_release.return_value = 'kilo'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-plugin-openvswitch-agent',
            'neutron-plugin-sriov-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dvr')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'os_release')
    @patch.object(charmhelpers.contrib.openstack.neutron, 'headers_package')
    def test_determine_pkgs_sriov_mitaka(self, _head_pkgs, _os_rel,
                                         _use_dvr, _use_l3ha):
        self.test_config.set('enable-local-dhcp-and-metadata', False)
        self.test_config.set('enable-sriov', True)
        _use_dvr.return_value = False
        _use_l3ha.return_value = False
        _os_rel.return_value = 'mitaka'
        self.os_release.return_value = 'mitaka'
        _head_pkgs.return_value = head_pkg
        pkg_list = nutils.determine_packages()
        expect = [
            head_pkg,
            'neutron-openvswitch-agent',
            'neutron-sriov-agent',
        ]
        self.assertEqual(pkg_list, expect)

    @patch.object(nutils, 'use_dvr')
    def test_register_configs(self, _use_dvr):
        class _mock_OSConfigRenderer():
            def __init__(self, templates_dir=None, openstack_release=None):
                self.configs = []
                self.ctxts = []

            def register(self, config, ctxt):
                self.configs.append(config)
                self.ctxts.append(ctxt)

        _use_dvr.return_value = False
        self.os_release.return_value = 'icehouse'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'precise'}
        templating.OSConfigRenderer.side_effect = _mock_OSConfigRenderer
        _regconfs = nutils.register_configs()
        confs = ['/etc/neutron/neutron.conf',
                 '/etc/neutron/plugins/ml2/ml2_conf.ini',
                 '/etc/default/openvswitch-switch',
                 '/etc/init/os-charm-phy-nic-mtu.conf']
        self.assertEqual(_regconfs.configs, confs)

    @patch.object(nutils, 'use_dvr')
    def test_register_configs_mitaka(self, _use_dvr):
        class _mock_OSConfigRenderer():
            def __init__(self, templates_dir=None, openstack_release=None):
                self.configs = []
                self.ctxts = []

            def register(self, config, ctxt):
                self.configs.append(config)
                self.ctxts.append(ctxt)

        _use_dvr.return_value = False
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'trusty'}
        templating.OSConfigRenderer.side_effect = _mock_OSConfigRenderer
        _regconfs = nutils.register_configs()
        confs = ['/etc/neutron/neutron.conf',
                 '/etc/neutron/plugins/ml2/openvswitch_agent.ini',
                 '/etc/init/os-charm-phy-nic-mtu.conf']
        self.assertEqual(_regconfs.configs, confs)

    @patch.object(nutils, 'use_dvr')
    def test_resource_map(self, _use_dvr):
        _use_dvr.return_value = False
        self.os_release.return_value = 'icehouse'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'precise'}
        _map = nutils.resource_map()
        svcs = ['neutron-plugin-openvswitch-agent']
        confs = [nutils.NEUTRON_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)

    @patch.object(nutils, 'enable_sriov')
    @patch.object(nutils, 'use_dvr')
    def test_resource_map_kilo_sriov(self, _use_dvr, _enable_sriov):
        _use_dvr.return_value = False
        _enable_sriov.return_value = True
        self.os_release.return_value = 'kilo'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'trusty'}
        _map = nutils.resource_map()
        svcs = ['neutron-plugin-openvswitch-agent',
                'neutron-plugin-sriov-agent']
        confs = [nutils.NEUTRON_CONF, nutils.NEUTRON_SRIOV_AGENT_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)
        self.assertEqual(_map[nutils.NEUTRON_SRIOV_AGENT_CONF]['services'],
                         ['neutron-plugin-sriov-agent'])

    @patch.object(nutils, 'use_dvr')
    def test_resource_map_mitaka(self, _use_dvr):
        _use_dvr.return_value = False
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        _map = nutils.resource_map()
        svcs = ['neutron-openvswitch-agent']
        confs = [nutils.NEUTRON_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)

    @patch.object(nutils, 'enable_sriov')
    @patch.object(nutils, 'use_dvr')
    def test_resource_map_mitaka_sriov(self, _use_dvr, _enable_sriov):
        _use_dvr.return_value = False
        _enable_sriov.return_value = True
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        _map = nutils.resource_map()
        svcs = ['neutron-openvswitch-agent',
                'neutron-sriov-agent']
        confs = [nutils.NEUTRON_CONF, nutils.NEUTRON_SRIOV_AGENT_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)
        self.assertEqual(_map[nutils.NEUTRON_SRIOV_AGENT_CONF]['services'],
                         ['neutron-sriov-agent'])

    @patch.object(nutils, 'use_dvr')
    def test_resource_map_dvr(self, _use_dvr):
        _use_dvr.return_value = True
        self.os_release.return_value = 'icehouse'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        _map = nutils.resource_map()
        svcs = ['neutron-plugin-openvswitch-agent', 'neutron-metadata-agent',
                'neutron-l3-agent']
        confs = [nutils.NEUTRON_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)

    @patch.object(nutils, 'enable_local_dhcp')
    @patch.object(nutils, 'use_dvr')
    def test_resource_map_dhcp(self, _use_dvr, _enable_local_dhcp):
        _enable_local_dhcp.return_value = True
        _use_dvr.return_value = False
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        _map = nutils.resource_map()
        svcs = ['neutron-metadata-agent', 'neutron-dhcp-agent',
                'neutron-openvswitch-agent']
        confs = [nutils.NEUTRON_CONF, nutils.NEUTRON_METADATA_AGENT_CONF,
                 nutils.NEUTRON_DHCP_AGENT_CONF]
        [self.assertIn(q_conf, _map.keys()) for q_conf in confs]
        self.assertEqual(_map[nutils.NEUTRON_CONF]['services'], svcs)

    @patch.object(nutils, 'use_dvr')
    def test_resource_map_mtu_trusty(self, _use_dvr):
        _use_dvr.return_value = False
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'trusty'}
        _map = nutils.resource_map()
        self.assertTrue(nutils.NEUTRON_CONF in _map.keys())
        self.assertTrue(nutils.PHY_NIC_MTU_CONF in _map.keys())
        self.assertFalse(nutils.EXT_PORT_CONF in _map.keys())
        _use_dvr.return_value = True
        _map = nutils.resource_map()
        self.assertTrue(nutils.EXT_PORT_CONF in _map.keys())

    @patch.object(nutils, 'use_dvr')
    def test_resource_map_mtu_xenial(self, _use_dvr):
        _use_dvr.return_value = False
        self.os_release.return_value = 'mitaka'
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        _map = nutils.resource_map()
        self.assertTrue(nutils.NEUTRON_CONF in _map.keys())
        self.assertFalse(nutils.PHY_NIC_MTU_CONF in _map.keys())
        self.assertFalse(nutils.EXT_PORT_CONF in _map.keys())
        _use_dvr.return_value = True
        _map = nutils.resource_map()
        self.assertFalse(nutils.EXT_PORT_CONF in _map.keys())

    @patch.object(nutils, 'use_l3ha')
    @patch.object(nutils, 'use_dpdk')
    @patch.object(nutils, 'use_dvr')
    def test_restart_map(self, mock_use_dvr, mock_use_dpdk, mock_use_l3ha):
        mock_use_dvr.return_value = False
        mock_use_l3ha.return_value = False
        mock_use_dpdk.return_value = False
        self.os_release.return_value = "mitaka"
        self.lsb_release.return_value = {'DISTRIB_CODENAME': 'xenial'}
        ML2CONF = "/etc/neutron/plugins/ml2/openvswitch_agent.ini"
        _restart_map = nutils.restart_map()
        expect = OrderedDict([
            (nutils.NEUTRON_CONF, ['neutron-openvswitch-agent']),
            (ML2CONF, ['neutron-openvswitch-agent']),
        ])
        for item in _restart_map:
            self.assertTrue(item in _restart_map)
            self.assertTrue(expect[item] == _restart_map[item])
        self.assertEqual(len(_restart_map.keys()), 2)

    @patch('charmhelpers.contrib.openstack.context.list_nics',
           return_value=['eth0'])
    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_ovs_data_port(self, mock_config, _use_dvr, _nics):
        _use_dvr.return_value = False
        self.is_linuxbridge_interface.return_value = False
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        _nics.return_value = ['eth0']
        self.ExternalPortContext.return_value = \
            DummyContext(return_value=None)
        # Test back-compatibility i.e. port but no bridge (so br-data is
        # assumed)
        self.test_config.set('data-port', 'eth0')
        nutils.configure_ovs()
        self.add_bridge.assert_has_calls([
            call('br-int', 'system'),
            call('br-ex', 'system'),
            call('br-data', 'system')
        ])
        self.assertTrue(self.add_bridge_port.called)

        # Now test with bridge:port format
        self.test_config.set('data-port', 'br-foo:eth0')
        self.add_bridge.reset_mock()
        self.add_bridge_port.reset_mock()
        nutils.configure_ovs()
        self.add_bridge.assert_has_calls([
            call('br-int', 'system'),
            call('br-ex', 'system'),
            call('br-data', 'system')
        ])
        # Not called since we have a bogus bridge in data-ports
        self.assertFalse(self.add_bridge_port.called)

    @patch('charmhelpers.contrib.openstack.context.list_nics',
           return_value=['eth0', 'br-juju'])
    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_data_port_with_bridge(
            self, mock_config, _use_dvr, _nics):
        _use_dvr.return_value = False
        self.is_linuxbridge_interface.return_value = True
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        self.ExternalPortContext.return_value = \
            DummyContext(return_value=None)

        # Now test with bridge:bridge format
        self.test_config.set('bridge-mappings', 'physnet1:br-foo')
        self.test_config.set('data-port', 'br-foo:br-juju')
        _nics.return_value = ['br-juju']
        self.add_bridge.reset_mock()
        self.add_bridge_port.reset_mock()
        nutils.configure_ovs()
        self.assertTrue(self.add_ovsbridge_linuxbridge.called)

    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_starts_service_if_required(self, mock_config,
                                                      _use_dvr):
        _use_dvr.return_value = False
        mock_config.side_effect = self.test_config.get
        self.config.return_value = 'ovs'
        self.service_running.return_value = False
        nutils.configure_ovs()
        self.assertTrue(self.full_restart.called)

    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_doesnt_restart_service(self, mock_config, _use_dvr):
        _use_dvr.return_value = False
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        self.service_running.return_value = True
        nutils.configure_ovs()
        self.assertFalse(self.full_restart.called)

    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_ovs_ext_port(self, mock_config, _use_dvr):
        _use_dvr.return_value = True
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        self.test_config.set('ext-port', 'eth0')
        self.ExternalPortContext.return_value = \
            DummyContext(return_value={'ext_port': 'eth0'})
        nutils.configure_ovs()
        self.add_bridge.assert_has_calls([
            call('br-int', 'system'),
            call('br-ex', 'system'),
            call('br-data', 'system')
        ])
        self.add_bridge_port.assert_called_with('br-ex', 'eth0')

    def _run_configure_ovs_dpdk(self, mock_config, _use_dvr,
                                _resolve_dpdk_bridges, _resolve_dpdk_bonds,
                                _late_init, _test_bonds,
                                _ovs_vhostuser_client=False):
        def _resolve_port_name(pci_address, device_index, late_init):
            if late_init:
                return 'dpdk-{}'.format(
                    hashlib.sha1(pci_address.encode('UTF-8')).hexdigest()[:7]
                )
            else:
                return 'dpdk{}'.format(device_index)
        if _test_bonds:
            _resolve_dpdk_bridges.return_value = OrderedDict()
            _resolve_dpdk_bonds.return_value = OrderedDict([
                ('0000:001c.01', 'bond0'),
                ('0000:001c.02', 'bond1'),
                ('0000:001c.03', 'bond2'),
            ])
            self.parse_data_port_mappings.return_value = OrderedDict([
                ('bond0', 'br-phynet1'),
                ('bond1', 'br-phynet2'),
                ('bond2', 'br-phynet3'),
            ])
        else:
            _resolve_dpdk_bridges.return_value = OrderedDict([
                ('0000:001c.01', 'br-phynet1'),
                ('0000:001c.02', 'br-phynet2'),
                ('0000:001c.03', 'br-phynet3'),
            ])
            _resolve_dpdk_bonds.return_value = OrderedDict()
        _use_dvr.return_value = True
        self.use_dpdk.return_value = True
        self.ovs_has_late_dpdk_init.return_value = _late_init
        self.ovs_vhostuser_client.return_value = _ovs_vhostuser_client
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        self.test_config.set('enable-dpdk', True)
        nutils.configure_ovs()
        self.add_bridge.assert_has_calls([
            call('br-int', 'netdev'),
            call('br-ex', 'netdev'),
            call('br-phynet1', 'netdev'),
            call('br-phynet2', 'netdev'),
            call('br-phynet3', 'netdev')],
            any_order=True
        )
        if _test_bonds:
            self.dpdk_add_bridge_bond.assert_has_calls([
                call('br-phynet1', 'bond0',
                     {_resolve_port_name('0000:001c.01',
                                         0, _late_init): '0000:001c.01'}),
                call('br-phynet2', 'bond1',
                     {_resolve_port_name('0000:001c.02',
                                         1, _late_init): '0000:001c.02'}),
                call('br-phynet3', 'bond2',
                     {_resolve_port_name('0000:001c.03',
                                         2, _late_init): '0000:001c.03'})],
                any_order=True
            )
            self.dpdk_set_bond_config.assert_has_calls([
                call('bond0',
                     {'mode': 'balance-tcp',
                      'lacp': 'active',
                      'lacp-time': 'fast'}),
                call('bond1',
                     {'mode': 'balance-tcp',
                      'lacp': 'active',
                      'lacp-time': 'fast'}),
                call('bond2',
                     {'mode': 'balance-tcp',
                      'lacp': 'active',
                      'lacp-time': 'fast'})],
                any_order=True
            )
            self.dpdk_set_interfaces_mtu.assert_has_calls([
                call(1500, {'dpdk-ac48d24': None}.keys()),
                call(1500, {'dpdk-82c1c9e': None}.keys()),
                call(1500, {'dpdk-aebdb4d': None}.keys())],
                any_order=True)
        else:
            self.dpdk_add_bridge_port.assert_has_calls([
                call('br-phynet1',
                     _resolve_port_name('0000:001c.01',
                                        0, _late_init),
                     '0000:001c.01'),
                call('br-phynet2',
                     _resolve_port_name('0000:001c.02',
                                        1, _late_init),
                     '0000:001c.02'),
                call('br-phynet3',
                     _resolve_port_name('0000:001c.03',
                                        2, _late_init),
                     '0000:001c.03')],
                any_order=True
            )
            self.dpdk_set_mtu_request.assert_has_calls([
                call(_resolve_port_name('0000:001c.01',
                                        0, _late_init), 1500),
                call(_resolve_port_name('0000:001c.02',
                                        1, _late_init), 1500),
                call(_resolve_port_name('0000:001c.03',
                                        2, _late_init), 1500)],
                any_order=True)

    @patch.object(neutron_ovs_context, 'NeutronAPIContext')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bonds')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bridges')
    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_dpdk(self, mock_config, _use_dvr,
                                _resolve_dpdk_bridges,
                                _resolve_dpdk_bonds,
                                _NeutronAPIContext):
        _NeutronAPIContext.return_value = DummyContext(
            return_value={'global_physnet_mtu': 1500})
        return self._run_configure_ovs_dpdk(mock_config, _use_dvr,
                                            _resolve_dpdk_bridges,
                                            _resolve_dpdk_bonds,
                                            _late_init=False,
                                            _test_bonds=False)

    @patch.object(neutron_ovs_context, 'NeutronAPIContext')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bonds')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bridges')
    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_dpdk_late_init(self, mock_config, _use_dvr,
                                          _resolve_dpdk_bridges,
                                          _resolve_dpdk_bonds,
                                          _NeutronAPIContext):
        _NeutronAPIContext.return_value = DummyContext(
            return_value={'global_physnet_mtu': 1500})
        return self._run_configure_ovs_dpdk(mock_config, _use_dvr,
                                            _resolve_dpdk_bridges,
                                            _resolve_dpdk_bonds,
                                            _late_init=True,
                                            _test_bonds=False)

    @patch.object(neutron_ovs_context, 'NeutronAPIContext')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bonds')
    @patch.object(neutron_ovs_context, 'resolve_dpdk_bridges')
    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_dpdk_late_init_bonds(self, mock_config, _use_dvr,
                                                _resolve_dpdk_bridges,
                                                _resolve_dpdk_bonds,
                                                _NeutronAPIContext):
        _NeutronAPIContext.return_value = DummyContext(
            return_value={'global_physnet_mtu': 1500})
        return self._run_configure_ovs_dpdk(mock_config, _use_dvr,
                                            _resolve_dpdk_bridges,
                                            _resolve_dpdk_bonds,
                                            _late_init=True,
                                            _test_bonds=True)

    @patch.object(nutils, 'use_dvr')
    @patch('charmhelpers.contrib.openstack.context.config')
    def test_configure_ovs_enable_ipfix(self, mock_config, mock_use_dvr):
        mock_use_dvr.return_value = False
        mock_config.side_effect = self.test_config.get
        self.config.side_effect = self.test_config.get
        self.test_config.set('ipfix-target', '127.0.0.1:80')
        nutils.configure_ovs()
        self.enable_ipfix.assert_has_calls([
            call('br-int', '127.0.0.1:80'),
            call('br-ex', '127.0.0.1:80'),
        ])

    @patch.object(neutron_ovs_context, 'SharedSecretContext')
    def test_get_shared_secret(self, _dvr_secret_ctxt):
        _dvr_secret_ctxt.return_value = \
            DummyContext(return_value={'shared_secret': 'supersecret'})
        self.assertEqual(nutils.get_shared_secret(), 'supersecret')

    def test_assess_status(self):
        with patch.object(nutils, 'assess_status_func') as asf:
            callee = MagicMock()
            asf.return_value = callee
            nutils.assess_status('test-config')
            asf.assert_called_once_with('test-config')
            callee.assert_called_once_with()
            self.os_application_version_set.assert_called_with(
                nutils.VERSION_PACKAGE
            )

    @patch.object(nutils, 'REQUIRED_INTERFACES')
    @patch.object(nutils, 'services')
    @patch.object(nutils, 'determine_ports')
    @patch.object(nutils, 'make_assess_status_func')
    @patch.object(nutils, 'enable_nova_metadata')
    def test_assess_status_func(self,
                                enable_nova_metadata,
                                make_assess_status_func,
                                determine_ports,
                                services,
                                REQUIRED_INTERFACES):
        services.return_value = 's1'
        determine_ports.return_value = 'p1'
        enable_nova_metadata.return_value = False
        REQUIRED_INTERFACES.copy.return_value = {'Test': True}
        nutils.assess_status_func('test-config')
        # ports=None whilst port checks are disabled.
        make_assess_status_func.assert_called_once_with(
            'test-config',
            {'Test': True},
            services='s1',
            ports=None)

    def test_pause_unit_helper(self):
        with patch.object(nutils, '_pause_resume_helper') as prh:
            nutils.pause_unit_helper('random-config')
            prh.assert_called_once_with(nutils.pause_unit, 'random-config')
        with patch.object(nutils, '_pause_resume_helper') as prh:
            nutils.resume_unit_helper('random-config')
            prh.assert_called_once_with(nutils.resume_unit, 'random-config')

    @patch.object(nutils, 'services')
    @patch.object(nutils, 'determine_ports')
    def test_pause_resume_helper(self, determine_ports, services):
        f = MagicMock()
        services.return_value = 's1'
        determine_ports.return_value = 'p1'
        with patch.object(nutils, 'assess_status_func') as asf:
            asf.return_value = 'assessor'
            nutils._pause_resume_helper(f, 'some-config')
            asf.assert_called_once_with('some-config')
            # ports=None whilst port checks are disabled.
            f.assert_called_once_with('assessor', services='s1', ports=None)

    def _configure_sriov_base(self, config):
        self.mock_config = MagicMock()
        self.config.side_effect = None
        self.config.return_value = self.mock_config
        self.mock_config.get.side_effect = lambda x: config.get(x)

        self.mock_eth_device = MagicMock()
        self.mock_eth_device.sriov = False
        self.mock_eth_device.interface_name = 'eth0'
        self.mock_eth_device.sriov_totalvfs = 0

        self.mock_sriov_device = MagicMock()
        self.mock_sriov_device.sriov = True
        self.mock_sriov_device.interface_name = 'ens0'
        self.mock_sriov_device.sriov_totalvfs = 64

        self.mock_sriov_device2 = MagicMock()
        self.mock_sriov_device2.sriov = True
        self.mock_sriov_device2.interface_name = 'ens49'
        self.mock_sriov_device2.sriov_totalvfs = 64

        self.pci_devices = {
            'eth0': self.mock_eth_device,
            'ens0': self.mock_sriov_device,
            'ens49': self.mock_sriov_device2,
        }

        mock_pci_devices = MagicMock()
        mock_pci_devices.pci_devices = [
            self.mock_eth_device,
            self.mock_sriov_device,
            self.mock_sriov_device2
        ]
        self.PCINetDevices.return_value = mock_pci_devices

        mock_pci_devices.get_device_from_interface_name.side_effect = \
            lambda x: self.pci_devices.get(x)

    @patch('shutil.copy')
    @patch('os.chmod')
    def test_configure_sriov_auto(self, _os_chmod, _sh_copy):
        self.os_release.return_value = 'mitaka'
        _config = {
            'enable-sriov': True,
            'sriov-numvfs': 'auto'
        }
        self._configure_sriov_base(_config)

        nutils.configure_sriov()

        self.mock_sriov_device.set_sriov_numvfs.assert_called_with(
            self.mock_sriov_device.sriov_totalvfs
        )
        self.mock_sriov_device2.set_sriov_numvfs.assert_called_with(
            self.mock_sriov_device2.sriov_totalvfs
        )
        self.assertTrue(self.remote_restart.called)

    @patch('shutil.copy')
    @patch('os.chmod')
    def test_configure_sriov_auto_mapping(self, _os_chmod, _sh_copy):
        self.os_release.return_value = 'mitaka'
        _config = {
            'enable-sriov': True,
            'sriov-numvfs': 'auto',
            'sriov-device-mappings': 'net1:ens49'
        }
        self._configure_sriov_base(_config)

        nutils.configure_sriov()

        self.assertFalse(self.mock_sriov_device.set_sriov_numvfs.called)
        self.mock_sriov_device2.set_sriov_numvfs.assert_called_with(
            self.mock_sriov_device2.sriov_totalvfs
        )
        self.assertTrue(self.remote_restart.called)

    @patch('shutil.copy')
    @patch('os.chmod')
    def test_configure_sriov_numvfs(self, _os_chmod, _sh_copy):
        self.os_release.return_value = 'mitaka'
        _config = {
            'enable-sriov': True,
            'sriov-numvfs': '32',
        }
        self._configure_sriov_base(_config)

        nutils.configure_sriov()

        self.mock_sriov_device.set_sriov_numvfs.assert_called_with(32)
        self.mock_sriov_device2.set_sriov_numvfs.assert_called_with(32)

        self.assertTrue(self.remote_restart.called)

    @patch('shutil.copy')
    @patch('os.chmod')
    def test_configure_sriov_numvfs_per_device(self, _os_chmod, _sh_copy):
        self.os_release.return_value = 'kilo'
        _config = {
            'enable-sriov': True,
            'sriov-numvfs': 'ens0:32 sriov23:64'
        }
        self._configure_sriov_base(_config)

        nutils.configure_sriov()

        self.mock_sriov_device.set_sriov_numvfs.assert_called_with(32)
        self.mock_sriov_device2.set_sriov_numvfs.assert_not_called()

        self.assertTrue(self.remote_restart.called)

    @patch('shutil.copy')
    @patch('os.chmod')
    def test_configure_sriov_auto_avoid_recall(self, _os_chmod, _sh_copy):
        self.os_release.return_value = 'mitaka'
        _config = {
            'enable-sriov': True,
            'sriov-numvfs': 'auto'
        }
        self._configure_sriov_base(_config)

        nutils.configure_sriov()

        self.mock_sriov_device2.sriov_numvfs = 64
        self.mock_sriov_device2.set_sriov_numvfs.assert_called_with(
            self.mock_sriov_device2.sriov_totalvfs)
        self.mock_sriov_device2._set_sriov_numvfs.assert_not_called()

        self.assertTrue(self.remote_restart.called)

    @patch.object(nutils, 'subprocess')
    @patch.object(nutils, 'shutil')
    def test_install_tmpfilesd_lxd(self, mock_shutil, mock_subprocess):
        self.init_is_systemd.return_value = True
        self.group_exists.return_value = False
        self.user_exists.return_value = False
        nutils.install_tmpfilesd()
        mock_shutil.copy.assert_not_called()
        mock_subprocess.check_call.assert_not_called()

    @patch.object(nutils, 'subprocess')
    @patch.object(nutils, 'shutil')
    def test_install_tmpfilesd_libvirt(self, mock_shutil, mock_subprocess):
        self.init_is_systemd.return_value = True
        self.group_exists.return_value = True
        self.user_exists.return_value = True
        nutils.install_tmpfilesd()
        mock_shutil.copy.assert_called_once()
        mock_subprocess.check_call.assert_called_once_with(
            ['systemd-tmpfiles', '--create']
        )

    @patch.object(nutils, 'is_unit_paused_set')
    @patch.object(nutils.subprocess, 'check_call')
    @patch.object(neutron_ovs_context, 'OVSDPDKDeviceContext')
    @patch.object(nutils, 'set_Open_vSwitch_column_value')
    def test_enable_ovs_dpdk(self,
                             _set_Open_vSwitch_column_value,
                             _OVSDPDKDeviceContext,
                             _check_call,
                             _is_unit_paused_set):
        mock_context = MagicMock()
        mock_context.cpu_mask.return_value = '0x03'
        mock_context.socket_memory.return_value = '4096,4096'
        mock_context.pci_whitelist.return_value = \
            '--pci-whitelist 00:0300:01'
        _OVSDPDKDeviceContext.return_value = mock_context
        _set_Open_vSwitch_column_value.return_value = True
        self.ovs_has_late_dpdk_init.return_value = True
        self.ovs_vhostuser_client.return_value = False
        _is_unit_paused_set.return_value = False
        nutils.enable_ovs_dpdk()
        _set_Open_vSwitch_column_value.assert_has_calls([
            call('other_config:dpdk-lcore-mask', '0x03'),
            call('other_config:dpdk-socket-mem', '4096,4096'),
            call('other_config:dpdk-init', 'true'),
            call('other_config:dpdk-extra',
                 '--vhost-owner libvirt-qemu:kvm --vhost-perm 0660 '
                 '--pci-whitelist 00:0300:01')
        ])
        _check_call.assert_called_once_with(
            nutils.UPDATE_ALTERNATIVES + [nutils.OVS_DPDK_BIN]
        )
        self.service_restart.assert_called_with('openvswitch-switch')

    @patch.object(nutils, 'is_unit_paused_set')
    @patch.object(nutils.subprocess, 'check_call')
    @patch.object(neutron_ovs_context, 'OVSDPDKDeviceContext')
    @patch.object(nutils, 'set_Open_vSwitch_column_value')
    def test_enable_ovs_dpdk_vhostuser_client(
            self,
            _set_Open_vSwitch_column_value,
            _OVSDPDKDeviceContext,
            _check_call,
            _is_unit_paused_set):
        mock_context = MagicMock()
        mock_context.cpu_mask.return_value = '0x03'
        mock_context.socket_memory.return_value = '4096,4096'
        mock_context.pci_whitelist.return_value = \
            '--pci-whitelist 00:0300:01'
        _OVSDPDKDeviceContext.return_value = mock_context
        _set_Open_vSwitch_column_value.return_value = True
        self.ovs_has_late_dpdk_init.return_value = True
        self.ovs_vhostuser_client.return_value = True
        _is_unit_paused_set.return_value = False
        nutils.enable_ovs_dpdk()
        _set_Open_vSwitch_column_value.assert_has_calls([
            call('other_config:dpdk-lcore-mask', '0x03'),
            call('other_config:dpdk-socket-mem', '4096,4096'),
            call('other_config:dpdk-init', 'true'),
            call('other_config:dpdk-extra',
                 '--pci-whitelist 00:0300:01')
        ])
        _check_call.assert_called_once_with(
            nutils.UPDATE_ALTERNATIVES + [nutils.OVS_DPDK_BIN]
        )
        self.service_restart.assert_called_with('openvswitch-switch')


class TestDPDKBridgeBondMap(CharmTestCase):

    def setUp(self):
        super(TestDPDKBridgeBondMap, self).setUp(nutils,
                                                 TO_PATCH)
        self.config.side_effect = self.test_config.get

    def test_add_port(self):
        ctx = nutils.DPDKBridgeBondMap()
        ctx.add_port("br1", "bond1", "port1", "00:00:00:00:00:01")
        ctx.add_port("br1", "bond1", "port2", "00:00:00:00:00:02")
        ctx.add_port("br1", "bond2", "port3", "00:00:00:00:00:03")
        ctx.add_port("br1", "bond2", "port4", "00:00:00:00:00:04")

        expected = [(
            'br1', {
                'bond1': {
                    'port1': '00:00:00:00:00:01',
                    'port2': '00:00:00:00:00:02'
                },
                'bond2': {
                    'port3': '00:00:00:00:00:03',
                    'port4': '00:00:00:00:00:04',
                },
            },
        )]

        self.assertEqual(ctx.items(), expected)


class TestDPDKBondsConfig(CharmTestCase):

    def setUp(self):
        super(TestDPDKBondsConfig, self).setUp(nutils, TO_PATCH)
        self.config.side_effect = self.test_config.get

    def test_get_bond_config(self):
        self.test_config.set('dpdk-bond-config',
                             ':active-backup bond1:balance-slb:off')
        bonds_config = nutils.DPDKBondsConfig()

        self.assertEqual(bonds_config.get_bond_config('bond0'),
                         {'mode': 'active-backup',
                          'lacp': 'active',
                          'lacp-time': 'fast'
                          })
        self.assertEqual(bonds_config.get_bond_config('bond1'),
                         {'mode': 'balance-slb',
                          'lacp': 'off',
                          'lacp-time': 'fast'
                          })


class TestMTURequest(CharmTestCase):

    def setUp(self):
        super(TestMTURequest, self).setUp(nutils, [])

    @patch.object(nutils, 'subprocess')
    def test_dpdk_set_mtu_request(self, mock_subprocess):
        nutils.dpdk_set_mtu_request("dpdk1", 9000)
        mock_subprocess.check_call.assert_called_once_with(
            ['ovs-vsctl', 'set', 'Interface', 'dpdk1', 'mtu_request=9000'])

    @patch.object(nutils, 'dpdk_set_mtu_request')
    def test_dpdk_set_interfaces_mtu(self, mock_dpdk_set_mtu_request):
        nutils.dpdk_set_interfaces_mtu('1234', ['nic1', 'nic2'])
        expected_calls = [
            call('nic1', '1234'),
            call('nic2', '1234')]
        mock_dpdk_set_mtu_request.assert_has_calls(expected_calls)

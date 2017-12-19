# Copyright 2017 the Isard-vdi project authors:
#      Alberto Larraz Dalmases
#      Josep Maria Viñolas Auquer
# License: AGPLv3

# coding=utf-8
"""
a module to control hypervisor functions and state. Overrides libvirt events and

"""

import socket
import time
from datetime import datetime
from io import StringIO
from collections import deque
from statistics import mean
from threading import Thread

import libvirt
import paramiko
from lxml import etree
import pandas as pd

from engine.services.lib.functions import state_and_cause_to_str, hostname_to_uri, try_socket
from engine.services.lib.functions import test_hypervisor_conn, timelimit, new_dict_from_raw_dict_stats
from engine.services.lib.functions import calcule_cpu_hyp_stats
from engine.services.log import *
from engine.config import *

TIMEOUT_QUEUE = 20
TIMEOUT_CONN_HYPERVISOR = 4 #int(CONFIG_DICT['HYPERVISORS']['timeout_conn_hypervisor'])


# > 1 is connected
HYP_STATUS_CONNECTED = 10

# 1 is ready
HYP_STATUS_READY = 1

# < 0 not connected and error
HYP_STATUS_ERROR_WHEN_CONNECT = -5
HYP_STATUS_ERROR_WHEN_CONNECT_TIMELIMIT = -6
HYP_STATUS_ERROR_NOT_RESOLVES_HOSTNAME = -7
HYP_STATUS_ERROR_WHEN_CLOSE_CONNEXION = -1
HYP_STATUS_NOT_ALIVE = -10



class hyp(object):
    """
    operates with hypervisor
    """
    def __init__(self, address, user='root', port=22, capture_events=False, try_ssh_autologin=False):

        # dictionary of domains
        # self.id = 0
        self.domains = {}
        if (type(port) == int) and port > 1 and port < pow(2, 16):
            self.port = port
        else:
            self.port = 22
        self.try_ssh_autologin = try_ssh_autologin
        self.user = user
        self.hostname = address
        self.connected = False
        self.ssh_autologin_fail = False
        self.fail_connected_reason = ''
        self.eventLoopThread = None
        self.info = {}
        self.info_stats = {}
        self.capture_events = capture_events

        self.create_stats_vars()

        # isard_preferred_keys = tuple(paramiko.ecdsakey.ECDSAKey.supported_key_format_identifiers()) + (
        #    'ssh-rsa',
        #    'ssh-dss',
        # )
        #
        # paramiko.Transport._preferred_keys = isard_preferred_keys

        if self.connect_to_hyp():
            if self.capture_events:
                self.launch_events()


    def try_ssh(self):

        # try socket
        timeout = float(CONFIG_DICT['TIMEOUTS']['ssh_paramiko_hyp_test_connection'])
        if try_socket(self.hostname, self.port, timeout):

            # ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh = paramiko.SSHClient()
            # ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            # INFO TO DEVELOPER: OJO, load_system_host_keys debería ir pero el problema está en que hay ciertos algoritmos de firma
            # que la librería actual de paramiko da error. Seguramente haciendo un update de la librearía en un futuro
            # esto se arreglará espero: si solo existe el hash ecdsa-sha2-nistp256
            # ssh -o "HostKeyAlgorithms ssh-rsa" root@ajenti.escoladeltreball.org

            ssh.load_system_host_keys()
            ssh.load_host_keys(os.path.expanduser('~/.ssh/known_hosts'))
            # time.sleep(1)
            try:
                # timelimit(3,test_hypervisor_conn,self.hostname,
                #             username=self.user,
                #             port= self.port,
                #             timeout=CONFIG_DICT['TIMEOUTS']['ssh_paramiko_hyp_test_connection'])
                ssh.connect(self.hostname,
                            username=self.user,
                            port=self.port,
                            timeout=timeout, banner_timeout=timeout)

                log.debug("host {} with ip {} TEST CONNECTION OK, ssh connect without password".format(self.hostname,
                                                                                                       self.ip))
                ssh.close()
            except paramiko.SSHException as e:
                log.error("host {} with ip {} can't connect with ssh without password. Reason: {}".format(self.hostname,
                                                                                                          self.ip, e))
                log.error("")
                self.fail_connected_reason = 'ssh authentication fail when connect: {}'.format(e)
                self.ssh_autologin_fail = True
            except Exception as e:
                log.error(
                    "host {} with ip {} can't connect with ssh without password. Reasons? timeout, ssh authentication with keys is needed, port is correct?".format(
                        self.hostname, self.ip))
                log.error('reason: {}'.format(e))
                self.fail_connected_reason = 'ssh authentication fail when connect: {}'.format(e)
                self.ssh_autologin_fail = True

        else:
            self.ssh_autologin_fail = True
            self.fail_connected_reason = 'socket error in ssh port, sshd disabled or firewall'
            log.error(
                'socket error, try if ssh is listen in hostname {} with ip address {} and port {}'.format(self.hostname,
                                                                                                          self.ip,
                                                                                                          self.port))

    def connect_to_hyp(self):

        try:
            self.ip = socket.gethostbyname(self.hostname)

            if self.try_ssh_autologin == True:
                self.try_ssh()

            if self.ssh_autologin_fail is False:
                try:
                    self.uri = hostname_to_uri(self.hostname, user=self.user, port=self.port)

                    timeout_libvirt = float(CONFIG_DICT['TIMEOUTS']['libvirt_hypervisor_timeout_connection'])
                    self.conn = timelimit(timeout_libvirt, test_hypervisor_conn, self.uri)

                    # timeout = float(CONFIG_DICT['TIMEOUTS']['ssh_paramiko_hyp_test_connection'])

                    if (self.conn != False):
                        self.connected = True
                        # prueba de alberto para que indique cuando ha caído y para que mantenga alive la conexión


                        # OJO INFO TO DEVELOPER
                        # self.startEvent()


                        # este setKeepAlive no tengo claro que haga algo, pero bueno...
                        # y al ponerlo da error lo dejo comentado, pero en futuro hay que quitar
                        # esta línea si no sabemos bien que hace...
                        # self.conn.setKeepAlive(5, 3)
                        log.debug("connected to hypervisor: %s" % self.hostname)
                        self.set_status(HYP_STATUS_CONNECTED)
                        self.fail_connected_reason = ''
                        # para que le de tiempo a los eventos a quedarse registrados hay que esperar un poquillo, ya
                        # que se arranca otro thread
                        # self.get_hyp_info()
                        return True
                    else:
                        log.error('libvirt can\'t connect to hypervisor {}'.format(self.hostname))
                        log.info("""connection to hypervisor fail, try policykit or permissions,
                              or try in the hypervisor if libvirtd service is started
                              (in Fedora/Centos: systemctl status libvirtd )
                              or if the port 22 is open""")
                        self.set_status(HYP_STATUS_ERROR_WHEN_CONNECT)
                        self.fail_connected_reason = 'Hypervisor policykit or permissions or libvirtd has not started'

                        return False

                # except TimeLimitExpired:
                #     log.error("""Time Limit Expired connecting to hypervisor""")
                #     self.set_status(HYP_STATUS_ERROR_WHEN_CONNECT_TIMELIMIT)
                #     self.fail_connected_reason = 'Time Limit Expired connecting to hypervisor'
                #     return False

                except Exception as e:
                    log.error('connection to hypervisor {} fail with unexpected error: {}'.format(self.hostname, e))
                    log.error('libvirt uri: {}'.format(self.uri))
                    self.set_status(HYP_STATUS_ERROR_WHEN_CONNECT)
                    self.fail_connected_reason = 'connection to hypervisor {} fail with unexpected error'.format(
                        self.hostname)
                    return False

        except socket.error as e:
            log.error(e)
            log.error('not resolves ip from hostname: {}'.format(self.hostname))
            self.fail_connected_reason = 'not resolves ip from hostname: {}'.format(self.hostname)
            return False

        except Exception as e:
            log.error(e)

    def launch_events(self):
        self.launch_event_handlers()
        self.conn.registerCloseCallback(self.myConnectionCloseCallback, None)

    def set_status(self, status_code):
        if status_code > 1:
            self.connected = True
        else:
            self.connected = False

            # set_hyp_status(self.hostname,status_code)



    def get_hyp_info(self):

        libvirt_version = str(self.conn.getLibVersion())
        self.info['libvirt_version'] = '{}.{}.{}'.format(int(libvirt_version[-9:-6]),
                                                         int(libvirt_version[-6:-3]),
                                                         int(libvirt_version[-3:]))

        qemu_version = str(self.conn.getVersion())
        self.info['qemu_version'] = '{}.{}.{}'.format(int(qemu_version[-9:-6]),
                                                      int(qemu_version[-6:-3]),
                                                      int(qemu_version[-3:]))

        inf = self.conn.getInfo()
        self.info['arch'] = inf[0]
        self.info['memory_in_MB'] = inf[1]
        self.info['cpu_threads'] = inf[2]
        self.info['cpu_mhz'] = inf[3]
        self.info['numa_nodes'] = inf[4]
        self.info['cpu_cores'] = inf[6]
        self.info['threads_x_core'] = inf[7]
        xml = self.conn.getSysinfo()
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.parse(StringIO(xml), parser)

        if tree.xpath('/sysinfo/processor/entry[@name="socket_destination"]'):
            self.info['cpu_model'] = tree.xpath('/sysinfo/processor/entry[@name="socket_destination"]')[0].text

        if tree.xpath('/sysinfo/system/entry[@name="manufacturer"]'):
            self.info['motherboard_manufacturer'] = tree.xpath('/sysinfo/system/entry[@name="manufacturer"]')[0].text

        if tree.xpath('/sysinfo/system/entry[@name="product"]'):
            self.info['motherboard_model'] = tree.xpath('/sysinfo/system/entry[@name="product"]')[0].text

        if tree.xpath('/sysinfo/memory_device'):
            self.info['memory_banks'] = len(tree.xpath('/sysinfo/memory_device'))
            self.info['memory_type'] = tree.xpath('/sysinfo/memory_device/entry[@name="type"]')[0].text
            self.info['memory_speed'] = tree.xpath('/sysinfo/memory_device/entry[@name="speed"]')[0].text

        xml = self.conn.getCapabilities()
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.parse(StringIO(xml), parser)

        if tree.xpath('/capabilities/host/cpu/model'):
            self.info['cpu_model_type'] = tree.xpath('/capabilities/host/cpu/model')[0].text

        if tree.xpath('/capabilities/guest/arch/domain[@type="kvm"]/machine[@canonical]'):
            self.info['kvm_type_machine_canonical'] = \
                tree.xpath('/capabilities/guest/arch/domain[@type="kvm"]/machine[@canonical]')[0].get('canonical')

    def define_and_start_paused_xml(self, xml_text):
        # todo alberto: faltan todas las excepciones, y mensajes de log,
        # aquí hay curro y es importante, porque hay que mirar si los discos no están
        # si es un error de conexión con el hypervisor...
        # está el tema de los timeouts...
        # TODO INFO TO DEVELOPER: igual se podría verificar si arrancando el dominio sin definirlo
        # con la opción XML_INACTIVE sería suficiente
        # o quizás lo mejor sería arrancar con createXML(libvirt.VIR_DOMAIN_START_PAUSED)
        xml_stopped = ''
        xml_started = ''
        try:
            d = self.conn.defineXML(xml_text)
            d.undefine()
            try:
                d = self.conn.createXML(xml_text, flags=libvirt.VIR_DOMAIN_START_PAUSED)
                xml_started = d.XMLDesc()
                xml_stopped = d.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
                d.destroy()
            except Exception as e:
                log.error('error starting paused vm: {}'.format(e))

        except Exception as e:
            log.error('error defining vm: {}'.format(e))

        return xml_stopped, xml_started

    def get_domains(self):
        """
        return dictionary with domain objects of libvirt
        keys of dictionary are names
        domains can be started or paused
        """
        if self.connected:
            self.domains = {}
            try:
                for d in h.conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
                    try:
                        domain_name = d.name()
                    except:
                        log.info('unkown domain fail when trying to get his name, power off??')
                        continue
                    if domain_name[0] == '_':
                        self.domains[domain_name] = d
            except:
                log.error('error when try to list domain in hypervisor {}'.format(self.hostname))
                self.domains = {}

    #
    # def hyp_worker_thread(self,queue_worker):
    #     log.debug('Hyp {} worker thread started ...'.format(self.hostname))
    #     #h = hyp('vdesktop1')
    #     while(1):
    #         try:
    #             d=queue_worker.get(timeout=TIMEOUT_QUEUE)
    #             log.debug('received ACTION:{}; '.format(d['action']))
    #             if d['action'] == 'start_domain':
    #                 xml = d['xml']
    #                 h.conn.createXML(xml)
    #                 log.debug('domain started {} ??'.format(d['name']))
    #             if d['action'] == 'hyp_info':
    #                 h.conn.get_hyp_info()
    #
    #             log.debug('hypervisor motherboard: {}'.format(h.info['motherboard_manufacturer']))
    #             #time.sleep(0.1)
    #         except Queue.Empty:
    #             try:
    #                 if h.conn.isAlive():
    #                     log.debug('hypervisor {} is alive'.format(host))
    #                 else:
    #                     log.debug('trying to reconnect hypervisor {}'.format(host))
    #                     h.connect_to_hyp()
    #                     if h.conn.isAlive():
    #                         log.debug('hypervisor {} is alive'.format(host))
    #
    #             except libvirtError:
    #                 log.debug('trying to reconnect hypervisor {}'.format(host))
    #                 h.connect_to_hyp()
    #                 if h.conn.isAlive():
    #                     log.debug('hypervisor {} is alive'.format(host))

    def disconnect(self):
        try:
            self.conn.close()
            self.set_status(HYP_STATUS_READY)
        except:
            log.error('error closing connexion for hypervisor {}'.format(self.hostname))
            self.set_status(HYP_STATUS_ERROR_WHEN_CLOSE_CONNEXION)

    def get_stats_from_libvirt(self, exclude_domains_not_isard=True):
        raw_stats = {}
        if self.connected:
            # get CPU Stats
            try:
                raw_stats['cpu'] = self.conn.getCPUStats(libvirt.VIR_NODE_CPU_STATS_ALL_CPUS)
            except:
                log.error('getCPUStats fail in hypervisor {}'.format(self.hostname))
                return False

            # get Memory Stats
            try:
                raw_stats['memory'] = self.conn.getMemoryStats(-1, 0)
            except:
                log.error('getAllDomainStats fail in hypervisor {}'.format(self.hostname))
                return False

            # get All Domain Stats
            try:
                # l_stats = self.conn.getAllDomainStats(flags=libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE)
                raw_stats['domains'] = {l[0].name(): {'stats': l[1],
                                                    'state': l[0].state(),
                                                    'd'    : l[0]}
                                      for l in
                                      self.conn.getAllDomainStats(flags=libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE)}

                raw_stats['time_utc'] = time.time()

                # remove stats from domains not started with _ (all domains in isard start with _)
                if exclude_domains_not_isard is True:
                    for domain_name in list(raw_stats['domains'].keys()):
                        if domain_name[0] != '_':
                            del raw_stats['domains'][domain_name]

            except:
                log.error('getAllDomainStats fail in hypervisor {}'.format(self.hostname))
                return False

            return raw_stats

        else:
            log.error('can not get stats from libvirt if hypervisor {} is not connected'.format(self.hostname))
            return False

    def process_hypervisor_stats(self, raw_stats):
        if len(self.info) == 0:
            self.get_hyp_info()

        now_raw_stats_hyp = {}
        now_raw_stats_hyp['cpu_stats'] = raw_stats['cpu']
        now_raw_stats_hyp['mem_stats'] = raw_stats['memory']
        now_raw_stats_hyp['time_utc'] = raw_stats['time_utc']
        now_raw_stats_hyp['datetime'] = timestamp = datetime.utcfromtimestamp(raw_stats['time_utc'])

        self.stats_raw_hyp.append(now_raw_stats_hyp)

        sum_vcpus, sum_memory, sum_domains, sum_memory_max, sum_disk_wr, \
        sum_disk_rd, sum_disk_wr_reqs, sum_disk_rd_reqs, sum_net_tx, sum_net_rx = self.process_domains_stats(raw_stats)

        if len(self.stats_raw_hyp) > 1:
            hyp_stats = {}

            hyp_stats['num_domains'] = sum_domains

            cpu_percents = calcule_cpu_hyp_stats(self.stats_raw_hyp[-2]['cpu_stats'],self.stats_raw_hyp[-1]['cpu_stats'])[0]

            hyp_stats['cpu_load'] = round(cpu_percents['used'],2)
            hyp_stats['cpu_iowait'] = round(cpu_percents['iowait'],2)
            hyp_stats['vcpus'] = sum_vcpus
            hyp_stats['vcpu_cpu_rate'] = round((sum_vcpus / self.info['cpu_threads']), 2)

            hyp_stats['mem_load_rate']    = round(((raw_stats['memory']['total'] -
                                              raw_stats['memory']['free'] -
                                              raw_stats['memory']['cached']) / raw_stats['memory']['total'] ) * 100, 2)
            hyp_stats['mem_cached_rate']  = round(raw_stats['memory']['cached'] / raw_stats['memory']['total'] * 100, 2)
            hyp_stats['mem_free_gb'] = round((raw_stats['memory']['cached'] + raw_stats['memory']['free']) / 1024 / 1024 , 2)

            hyp_stats['mem_domains_gb'] = round(sum_memory, 3)
            hyp_stats['mem_domains_max_gb'] = round(sum_memory_max, 3)
            if sum_memory_max > 0:
                hyp_stats['mem_balloon_rate'] = round(sum_memory / sum_memory_max * 100, 2)
            else:
                hyp_stats['mem_balloon_rate'] = 0

            hyp_stats['disk_wr'] = sum_disk_wr
            hyp_stats['disk_rd'] = sum_disk_rd
            hyp_stats['disk_wr_reqs'] = sum_disk_wr_reqs
            hyp_stats['disk_rd_reqs'] = sum_disk_rd_reqs
            hyp_stats['net_tx'] = sum_net_tx
            hyp_stats['net_rx'] = sum_net_rx

            self.stats_hyp_now = hyp_stats.copy()

            fields = ['num_domains', 'vcpus', 'vcpu_cpu_rate', 'cpu_load', 'cpu_iowait',
                      'mem_load_rate', 'mem_free_gb', 'mem_cached_rate',
                      'mem_balloon_rate', 'mem_domains_gb', 'mem_domains_max_gb',
                      'disk_wr', 'disk_rd', 'disk_wr_reqs', 'disk_rd_reqs',
                      'net_tx', 'net_rx', ]

            self.stats_hyp = self.stats_hyp.append(pd.DataFrame(hyp_stats,
                                                                columns=fields,
                                                                index=[timestamp]))

    def process_domains_stats(self, raw_stats):
        if len(self.info) == 0:
            self.get_hyp_info()
        d_all_domain_stats = raw_stats['domains']

        previous_domains = set(self.stats_domains.keys())
        current_domains  = set(d_all_domain_stats.keys())
        add_domains      = current_domains.difference(previous_domains)
        remove_domains   = previous_domains.difference(current_domains)

        for d in remove_domains:
            del self.stats_domains[d]
            del self.stats_raw_domains[d]
            del self.stats_domains_now[d]
            # TODO: buen momento para asegurarse que la máquina se quedó en Stopped,
            # podríamos ahorrarnos esa  comprobación en el thread broom ??

            #TODO: también es el momento de guardar en histórico las estadísticas de ese dominio,
            # esto nos permite hacer análisis posterior

        for d in add_domains:
            self.stats_domains[d] = pd.DataFrame()
            self.stats_raw_domains[d] = deque(maxlen=self.stats_queue_lenght_domains_raw_stats)
            self.stats_domains_now[d] = dict()

        sum_vcpus = 0
        sum_memory = 0
        sum_domains = 0
        sum_memory_max = 0
        sum_disk_wr = 0
        sum_disk_rd = 0
        sum_net_tx = 0
        sum_net_rx = 0
        sum_disk_wr_reqs = 0
        sum_disk_rd_reqs = 0

        for d, raw in d_all_domain_stats.items():
            raw['stats']['now_utc_time'] = raw_stats['time_utc']
            raw['stats']['now_datetime'] = datetime.utcfromtimestamp(raw_stats['time_utc'])

            self.stats_raw_domains[d].append(raw['stats'])


            if len(self.stats_raw_domains[d]) > 1:

                sum_domains += 1

                d_stats = {}

                current = self.stats_raw_domains[d][-1]
                previous = self.stats_raw_domains[d][-2]

                delta = current['now_utc_time'] - previous['now_utc_time']
                if delta == 0:
                    log.error('same value in now_utc_time, must call get_stats_from_libvirt')
                    break

                timestamp = datetime.utcfromtimestamp(current['now_utc_time'])

                sum_vcpus += current['vcpu.current']

                if current.get('cpu.time') and previous.get('cpu.time'):
                    d_stats['cpu_load'] = round((current['cpu.time'] - previous['cpu.time']) / 1000000000 / self.info['cpu_threads'],3)

                d_balloon={k:v/1024 for k,v in current.items() if k[:5] == 'ballo' }

                # balloon is running and monitorized if balloon.unused key is disposable
                if 'balloon.unused' in d_balloon.keys():
                    mem_used    = round((d_balloon['balloon.current']-d_balloon['balloon.unused'])/1024.0, 3)
                    mem_balloon = round(d_balloon['balloon.current'] / 1024.0, 3)
                    mem_max = round(d_balloon['balloon.maximum'] / 1024.0, 3)

                elif 'balloon.maximum' in d_balloon.keys():
                    mem_used =    round(d_balloon['balloon.maximum'] / 1024.0 ,3)
                    mem_balloon = round(d_balloon['balloon.maximum'] / 1024.0, 3)
                    mem_max = round(d_balloon['balloon.maximum'] / 1024.0, 3)

                else:
                    mem_used = 0
                    mem_balloon = 0
                    mem_max = 0

                d_stats['mem_load']    = round(mem_used / (self.info['memory_in_MB']/1024), 2)
                d_stats['mem_used']    = mem_used
                d_stats['mem_balloon'] = mem_balloon
                d_stats['mem_max']     = mem_max

                sum_memory += mem_used
                sum_memory_max += mem_max

                total_block_wr = 0
                total_block_wr_reqs = 0
                total_block_rd = 0
                total_block_rd_reqs = 0

                if 'block.count' in current.keys():
                    values = [(".wr.bytes",total_block_wr) ,
                              (".rd.bytes", total_block_rd),
                              (".wr.reqs", total_block_wr_reqs),
                              (".rd.reqs", total_block_rd_reqs)]
                    for n in range(current['block.count']):
                        for value, var in values:
                            current_value = current.get('block.'+str(n)+value)
                            previous_value = previous.get('block.'+str(n)+value)
                            if current_value and previous_value:
                                var += current_value - previous_value
                        # total_block_wr += current['block.'+str(n)+'.wr.bytes'] - previous['block.'+str(n)+'.wr.bytes']
                        # total_block_rd += current['block.'+str(n)+'.rd.bytes'] - previous['block.'+str(n)+'.rd.bytes']
                        # total_block_wr_reqs += current['block.'+str(n)+'.wr.reqs'] - previous['block.'+str(n)+'.wr.reqs']
                        # total_block_rd_reqs += current['block.'+str(n)+'.rd.reqs'] - previous['block.'+str(n)+'.rd.reqs']


                #KB/s
                d_stats['disk_wr'] = round(total_block_wr / delta / 1024,3)
                d_stats['disk_rd'] = round(total_block_rd / delta / 1024,3)
                d_stats['disk_wr_reqs'] = round(total_block_wr_reqs / delta,3)
                d_stats['disk_rd_reqs'] = round(total_block_rd_reqs / delta,3)
                sum_disk_wr      += d_stats['disk_wr']
                sum_disk_rd      += d_stats['disk_rd']
                sum_disk_wr_reqs += d_stats['disk_wr_reqs']
                sum_disk_rd_reqs += d_stats['disk_rd_reqs']

                total_net_tx = 0
                total_net_rx = 0

                if 'net.count' in current.keys():
                    values = [(".tx.bytes", total_net_tx),
                              (".rx.bytes", total_net_rx)]
                    for n in range(current['net.count']):
                        for value, var in values:
                            current_value = current.get('net.' + str(n) + value)
                            previous_value = previous.get('net.' + str(n) + value)
                            if current_value and previous_value:
                                var += current_value - previous_value
                        # total_net_tx += current['net.' +str(n)+ '.tx.bytes'] - previous['net.' +str(n)+ '.tx.bytes']
                        # total_net_rx += current['net.' +str(n)+ '.rx.bytes'] - previous['net.' +str(n)+ '.rx.bytes']

                d_stats['net_tx'] = round(total_net_tx / delta / 1000, 3)
                d_stats['net_rx'] = round(total_net_rx / delta / 1000, 3)
                sum_net_tx += d_stats['net_tx']
                sum_net_rx += d_stats['net_rx']


                fields = "cpu_load / mem_load / mem_used / mem_balloon / mem_max / " + \
                         "disk_wr / disk_rd / disk_wr_reqs / disk_rd_reqs / net_tx / net_rx"
                fields = [s.strip() for s in fields.split('/')]

                self.stats_domains_now[d] = d_stats.copy()
                self.stats_domains[d] = self.stats_domains[d].append(pd.DataFrame(d_stats,
                                                                                  columns=fields,
                                                                                  index=[timestamp]))

        return sum_vcpus, sum_memory, sum_domains, sum_memory_max, sum_disk_wr, sum_disk_rd, \
               sum_disk_wr_reqs, sum_disk_rd_reqs, sum_net_tx, sum_net_rx


    def create_stats_vars(self):

        self.stats_queue_lenght_hyp_raw_stats = 3
        self.stats_queue_lenght_domains_raw_stats = 3

        self.stats_polling_interval = 5

        self.stats_booting_time = 120
        self.stats_near_size_window = 300
        self.stats_medium_size_window = 2 * 3600
        self.stats_long_size_window = 24 * 3600

        self.stats_near_sample_period = self.stats_polling_interval
        self.stats_medium_sample_period = 60
        self.stats_max_history = 600

        self.stats_hyp_now = dict()
        self.stats_domains_now = dict()
        self.stats_raw_hyp = deque(maxlen=self.stats_queue_lenght_hyp_raw_stats)
        self.stats_raw_domains = dict()

        #Pandas dataframe
        self.stats_hyp = pd.DataFrame()

        #Dictionary of pandas dataframes
        self.stats_domains = dict()


    def get_load(self):

        if len(self.info) == 0:
            self.get_hyp_info()

        raw_stats = self.get_stats_from_libvirt()

        if raw_stats is False:
            return False

        self.process_hypervisor_stats(raw_stats)

        return True
            #
            #
            #
            #h.get_domains()
            # d=h.domains['_admin_win7-admin']
            # d.setMemoryFlags(2684*1024,VIR_DOMAIN_AFFECT_LIVE)
            # 1334/110:
            # h=hyp('vdesktop4')
            # h.get_domains()
            # while True:
            #     sleep(1)
            #     d_balloon=[{k:v/1024 for k,v in d[1].items() if k[:5] == 'ballo' } for d in h.conn.getAllDomainStats() if d[0].name() == '_admin_win7-admin' ][0]
            #     pprint(d_balloon)
            #     print("in GB: {0:.2f}".format((d_balloon['balloon.available']-d_balloon['balloon.unused'])/1024))
            #     print("in GB: {0:.2f}".format((d_balloon['balloon.current']-d_balloon['balloon.unused'])/1024))
            #     print("in MB: {0:.2f}".format(d_balloon['balloon.available']-d_balloon['balloon.unused']-(d_balloon['balloon.available']-d_balloon['balloon.current'])))

            #
            #
            #
            #
            # #todo VER QUE HACEMOS CON ESTO...
            # self.load['cpu_load'] = cpu_load
            # self.load['ram_cached'] = memory_stats['cached']
            # self.load['ram_free'] = memory_stats['free']
            #
            # self.load['free_ram_total'] = memory_stats['cached'] + memory_stats['free']
            # self.load['percent_free'] = round(float(self.load['free_ram_total'])*100/memory_stats['total'],2)
            #
            #
            #
            #
            #
            #
            #
            # now = time.time()
            #
            #
            # #
            # #     5:
            # #     15:
            # #
            # #
            # # HYPERVISORS
            #
            # if d_all_domain_stats:
            #     self.load['total_vm'] = len(d_all_domain_stats)
            #
            #     vm_mem_max_total = 0
            #     vm_mem_with_ballon_total = 0
            #     vm_vcpus_total = 0
            #
            #     for domain_sysname,d_info in d_all_domain_stats.items():
            #         try:
            #             #todo VER QUE HACEMOS SI ESTÁ EN PAUSA, YA QUE TIENE RAM RESERVADA??
            #             # libvirt.VIR_DOMAIN_PAUSED
            #             raw_stats = d_info['stats']
            #             domain_state = d_info['state']
            #
            #
            #             self.domains_stats[domain_sysname]=dict()
            #             self.domains_stats[domain_sysname]['raw_stats'] = raw_stats
            #             self.domains_stats[domain_sysname]['hyp'] = self.hostname
            #
            #             state,reason = state_and_cause_to_str(domain_state[0],domain_state[1])
            #             self.domains_stats[domain_sysname]['state'] = state
            #             self.domains_stats[domain_sysname]['state_reason'] = reason
            #             try:
            #                 self.domains_stats[domain_sysname]['procesed_stats'] = new_dict_from_raw_dict_stats(raw_stats)
            #             except Exception as e:
            #                 log.warning('Procesing stats for domain {} with state {}({}) failed'.format(domain_sysname,state,reason))
            #
            #             #stats_json = json.dumps(r[1])
            #             if 'cpu.time' in raw_stats.keys():
            #                 time_cpu = r[1]['cpu.time']
            #             else:
            #                 time_cpu = 0
            #
            #             #self.domain_stats[domain_sysname]['cputime'] = time_cpu
            #
            #             if state == 'running':
            #                 # vm_mem_max_total += r[1]['balloon.maximum']
            #                 # if 'balloon.current' in r[1].keys():
            #                 #     vm_mem_with_ballon_total += r[1]['balloon.current']
            #                 vm_vcpus_total += r[1]['vcpu.current']
            #
            #         except libvirt.libvirtError as e:
            #
            #             log.error('libvirt Error getting domains in hyp class. Other thread stop domain?? {}'.format(e))
            #         except Exception as e:
            #             log.error(e)
            #
            #     self.load['vm_mem_max_total'] = int(vm_mem_max_total / 1024)
            #     self.load['vm_mem_with_ballon_total'] = int(vm_mem_with_ballon_total / 1024)
            #     self.load['vm_vcpus_total'] = vm_vcpus_total
            #
            #
            #
            # else:
            #     #log.debug('hyp {} have no vms or stats has failed'.format(self.hostname))
            #     pass

    def get_eval_statistics(self):
        self.get_load()
        if not self.last_load:
            self.last_load = self.load
            cpu_percent_free = 100
        else:
            cpu_percent = calcule_cpu_stats(self.last_load['cpu_load'], self.load['cpu_load'])[0]
            cpu_percent_free = cpu_percent["idle"]

        # Do mean of 3 cpu values. Doing this because CPU load is very sensitive.
        if len(self.cpu_percent_free) == 3:
            self.cpu_percent_free.pop()
        self.cpu_percent_free.append(cpu_percent_free)

        self.last_load = self.load
        self.get_domains()
        data = {"cpu_percent_free": round(mean(self.cpu_percent_free), 2),
                "ram_percent_free": self.load.get("percent_free"),
                "domains": list(self.domains.keys())}
        return data

    # def launch_eval_statistics(self, interval=1):
    #     self.running_eval_statistics = True
    #     t = Thread(target=self._refresh_ux_eval_statistics, args=(interval,))
    #     t.start()
    #
    # def stop_eval_statistics(self):
    #     self.running_eval_statistics = False
    #
    # def _refresh_ux_eval_statistics(self, interval):
    #     while (self.running_eval_statistics):
    #         self.last_load = self.load  # First time load is {}
    #         self.last_domain_stats = self.domain_stats  # First time domain_stats is {}
    #         self.get_load()
    #         time.sleep(interval)
    #     # OUT OF WHILE, thread finished
    #     self.last_load = None
    #     self.last_domain_stats = None

    def get_ux_eval_statistics(self, domain_id):
        """
        :param domain_id:
        :return: data {"ram_hyp_usage", "cpu_hyp_usage", "cpu_hyp_iowait", "cpu_usage"} when they are available

        fields = ['num_domains', 'vcpus', 'vcpu_cpu_rate', 'cpu_load', 'cpu_iowait',
                      'mem_load_rate', 'mem_free_gb', 'mem_cached_rate',
                      'mem_balloon_rate', 'mem_domains_gb','mem_domains_max_gb',
                      'disk_wr', 'disk_rd', 'disk_wr_reqs', 'disk_rd_reqs',
                      'net_tx', 'net_rx', ]
        self.stats_hyp_now.get('mem_load_rate')
        """
        data = {}
        data["ram_hyp_usage"] = self.stats_hyp_now.get('mem_load_rate')
        data["cpu_hyp_usage"] = self.stats_hyp_now.get('cpu_load')
        data["cpu_hyp_iowait"] = self.stats_hyp_now.get('cpu_iowait')
        domain_stats = self.stats_domains_now.get(domain_id)
        if domain_stats:
            data["cpu_usage"] = domain_stats.get('cpu_load')
        return data

import unittest
import docker
import requests.exceptions
import tempfile
import os
import time
import shutil
import subprocess
import sys
import pykafka
import semantic_version
import platform
# Tests for crawlers in kraken crawlers configuration.

from safe_containers_crawler import SafeContainersCrawler
from worker import Worker
from emitters_manager import EmittersManager
from utils.dockerutils import get_docker_container_rootfs_path
from utils.dockerutils import _fix_version
from utils.dockerutils import _get_docker_server_version

import logging

# Tests conducted with a single container running.


class SafeContainersCrawlerTests(unittest.TestCase):

    def setUp(self):
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        root.addHandler(ch)

        self.docker = docker.APIClient(base_url='unix://var/run/docker.sock',
                                       version='auto')
        try:
            if len(self.docker.containers()) != 0:
                raise Exception(
                    "Sorry, this test requires a machine with no docker"
                    "containers running.")
        except requests.exceptions.ConnectionError:
            print ("Error connecting to docker daemon, are you in the docker"
                   "group? You need to be in the docker group.")

        self.version_check()
        self.setup_plugincont_testing2()
        self.start_crawled_container()
        # start a kakfa+zookeeper container to send data to (to test our
        # kafka emitter)
        self.start_kafka_container()

    def setup_plugincont_testing2(self):
        plugincont_image_path = os.getcwd() + \
            '/crawler/utils/plugincont/plugincont_img'
        shutil.copyfile(
            plugincont_image_path + '/requirements.txt.testing',
            plugincont_image_path + '/requirements.txt')
        _platform = platform.linux_distribution()
        if _platform[0] != 'Ubuntu' or _platform[1] < '16.04':
            self.seccomp = False
            src_file = os.getcwd() + \
                '/crawler/plugin_containers_manager.py'
            os.system("sed -i.bak '/security_opt=/d; "
                      "/self._add_iptable_rules_in/d' " + src_file)
        else:
            self.seccomp = True

    def fix_test_artifacts(self):
        plugincont_image_path = os.getcwd() + \
            '/crawler/utils/plugincont/plugincont_img'
        shutil.copyfile(
            plugincont_image_path + '/requirements.txt.template',
            plugincont_image_path + '/requirements.txt')
        if self.seccomp is False:
            src_file = os.getcwd() + \
                '/crawler/plugin_containers_manager.py.bak'
            dst_file = os.getcwd() + \
                '/crawler/plugin_containers_manager.py'
            shutil.move(src_file, dst_file)
        pass

    def version_check(self):
        self.version_ok = False
        VERSION_SPEC = semantic_version.Spec('>=1.12.1')
        server_version = _get_docker_server_version()
        if VERSION_SPEC.match(semantic_version.Version(_fix_version(
                                                       server_version))):
            self.version_ok = True

    def start_kafka_container(self):
        self.docker.pull(repository='spotify/kafka', tag='latest')
        self.kafka_container = self.docker.create_container(
            image='spotify/kafka', ports=[9092, 2181],
            host_config=self.docker.create_host_config(port_bindings={
                9092: 9092,
                2181: 2181
            }),
            environment={'ADVERTISED_HOST': 'localhost',
                         'ADVERTISED_PORT': '9092'})
        self.docker.start(container=self.kafka_container['Id'])

    def start_crawled_container(self):
        # start a container to be crawled
        self.docker.pull(repository='ruby', tag='latest')
        self.container = self.docker.create_container(
            image='ruby:latest', command='tail -f /dev/null',
            ports=[8192],
            host_config=self.docker.create_host_config(port_bindings={
                8192: 8192,
            }),
            environment={'ADVERTISED_HOST': 'localhost',
                         'ADVERTISED_PORT': '8192'})
        self.tempd = tempfile.mkdtemp(prefix='crawlertest.')
        self.docker.start(container=self.container['Id'])
        time.sleep(5)
        rootfs = get_docker_container_rootfs_path(self.container['Id'])
        fd = open(rootfs + '/crawlplugins', 'w')
        fd.write('cpu\n')
        fd.write('os\n')
        fd.write('memory\n')
        fd.write('interface\n')
        fd.write('process\n')
        fd.write('rubypackage\n')
        fd.close()

    def tearDown(self):
        self.fix_test_artifacts()
        self.remove_crawled_container()
        self.remove_kafka_container()
        shutil.rmtree(self.tempd)

    def remove_kafka_container(self):
        self.docker.stop(container=self.kafka_container['Id'])
        self.docker.remove_container(container=self.kafka_container['Id'])

    def remove_crawled_container(self):
        self.docker.stop(container=self.container['Id'])
        self.docker.remove_container(container=self.container['Id'])

    def _testCrawlContainer1(self):
        if self.version_ok is False:
            pass
            return
        crawler = SafeContainersCrawler(
            features=[], user_list=self.container['Id'])
        frames = list(crawler.crawl())
        output = str(frames[0])
        print output  # only printed if the test fails
        assert 'interface-lo' in output
        assert 'if_octets_tx' in output
        assert 'cpu-0' in output
        assert 'cpu_nice' in output
        assert 'memory' in output
        assert 'memory_buffered' in output
        assert 'os' in output
        assert 'linux' in output
        assert 'process' in output
        assert 'tail' in output
        assert 'plugincont_user' in output
        assert 'rubypackage' in output
        assert 'rake' in output

    def _testCrawlContainer2(self):
        if self.version_ok is False:
            pass
            return
        env = os.environ.copy()
        mypath = os.path.dirname(os.path.realpath(__file__))
        os.makedirs(self.tempd + '/out')

        # crawler itself needs to be root
        process = subprocess.Popen(
            [
                '/usr/bin/python', mypath + '/../../crawler/crawler.py',
                '--url', 'file://' + self.tempd + '/out/crawler',
                '--features', 'none',
                '--crawlContainers', self.container['Id'],
                '--format', 'graphite',
                '--crawlmode', 'OUTCONTAINERSAFE',
            ],
            env=env)
        time.sleep(30)
        stdout, stderr = process.communicate()
        assert process.returncode == 0

        print stderr
        print stdout

        subprocess.call(['/bin/chmod', '-R', '777', self.tempd])

        files = os.listdir(self.tempd + '/out')
        assert len(files) == 1

        f = open(self.tempd + '/out/' + files[0], 'r')
        output = f.read()
        print output  # only printed if the test fails
        assert 'interface-lo.if_octets.tx' in output
        assert 'cpu-0.cpu-idle' in output
        assert 'memory.memory-used' in output
        f.close()

    def testCrawlContainerNoPlugins(self):
        if self.version_ok is False:
            pass
            return
        rootfs = get_docker_container_rootfs_path(self.container['Id'])
        fd = open(rootfs + '/crawlplugins', 'w')
        fd.write('noplugin\n')
        fd.close()

        env = os.environ.copy()
        mypath = os.path.dirname(os.path.realpath(__file__))
        os.makedirs(self.tempd + '/out')

        # crawler itself needs to be root
        process = subprocess.Popen(
            [
                '/usr/bin/python', mypath + '/../../crawler/crawler.py',
                '--url', 'file://' + self.tempd + '/out/crawler',
                '--features', 'none',
                '--crawlContainers', self.container['Id'],
                '--crawlmode', 'OUTCONTAINERSAFE',
            ],
            env=env)
        time.sleep(30)
        stdout, stderr = process.communicate()
        assert process.returncode == 0

        print stderr
        print stdout

        subprocess.call(['/bin/chmod', '-R', '777', self.tempd])

        files = os.listdir(self.tempd + '/out')
        assert len(files) == 1

        f = open(self.tempd + '/out/' + files[0], 'r')
        output = f.read()
        print output  # only printed if the test fails
        assert 'metadata' in output
        assert 'interface-lo' not in output
        assert 'cpu-0' not in output
        assert 'memory' not in output
        f.close()

    def testCrawlContainerKafka(self):
        # import pdb
        # pdb.set_trace()
        if self.version_ok is False:
            pass
            return
        env = os.environ.copy()
        mypath = os.path.dirname(os.path.realpath(__file__))
        os.makedirs(self.tempd + '/out')

        # crawler itself needs to be root
        process = subprocess.Popen(
            [
                '/usr/bin/python', mypath + '/../../crawler/crawler.py',
                '--url', 'kafka://localhost:9092/test',
                '--features', 'none',
                '--crawlContainers', self.container['Id'],
                '--crawlmode', 'OUTCONTAINERSAFE',
                '--numprocesses', '1'
            ],
            env=env)
        time.sleep(30)

        print self.docker.containers()
        stdout, stderr = process.communicate()
        assert process.returncode == 0

        print stderr
        print stdout

        kafka = pykafka.KafkaClient(hosts='localhost:9092')
        topic = kafka.topics['test']
        consumer = topic.get_simple_consumer()
        message = consumer.consume()
        print message.value
        assert '"cmd":"tail -f /dev/null"' in message.value
        assert 'interface-lo' in message.value
        assert 'if_octets_tx' in message.value
        assert 'cpu-0' in message.value
        assert 'cpu_nice' in message.value
        assert 'memory' in message.value
        assert 'memory_buffered' in message.value
        assert 'os' in message.value
        assert 'linux' in message.value
        assert 'process' in message.value
        assert 'tail' in message.value
        assert 'plugincont_user' in message.value
        assert 'rubypackage' in message.value
        assert 'rake' in message.value

    def _setup_plugincont_testing1(self):
        plugincont_name = '/plugin_cont_' + self.container['Id']
        for container in self.docker.containers():
            if plugincont_name in container['Names']:
                plugincont_id = container['Id']
        exec_instance = self.docker.exec_create(
            container=plugincont_id,
            user='root',
            cmd='pip install python-ptrace')
        self.docker.exec_start(exec_instance.get("Id"))

    def testCrawlContainerEvilPlugin(self):
        if self.version_ok is False:
            pass
            return
        rootfs = get_docker_container_rootfs_path(self.container['Id'])
        fd = open(rootfs + '/crawlplugins', 'w')
        fd.write('evil\n')
        fd.close()

        env = os.environ.copy()
        mypath = os.path.dirname(os.path.realpath(__file__))
        os.makedirs(self.tempd + '/out')

        # crawler itself needs to be root
        process = subprocess.Popen(
            [
                '/usr/bin/python', mypath + '/../../crawler/crawler.py',
                '--url', 'file://' + self.tempd + '/out/crawler',
                '--features', 'none',
                '--crawlContainers', self.container['Id'],
                '--crawlmode', 'OUTCONTAINERSAFE',
            ],
            env=env)
        time.sleep(30)
        stdout, stderr = process.communicate()
        assert process.returncode == 0

        print self.docker.containers()
        print stderr
        print stdout

        subprocess.call(['/bin/chmod', '-R', '777', self.tempd])

        files = os.listdir(self.tempd + '/out')
        assert len(files) == 1

        f = open(self.tempd + '/out/' + files[0], 'r')
        output = f.read()
        f.close()
        print output  # only printed if the test fails
        assert 'kill_status' in output
        assert 'trace_status' in output
        assert 'write_status' in output
        assert 'rm_status' in output
        assert 'nw_status' in output
        assert 'expected_failed' in output
        ctr = output.count('unexpected_succeeded')
        if self.seccomp is True:
            assert ctr == 0
        else:
            assert ctr == 1


if __name__ == '__main__':
    unittest.main()
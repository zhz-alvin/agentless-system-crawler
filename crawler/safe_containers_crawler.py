import ast
import sys
import docker
import iptc
from containers import poll_containers, get_containers
import plugins_manager
from base_crawler import BaseCrawler, BaseFrame
import utils.dockerutils


class ContainerFrame(BaseFrame):

    def __init__(self, feature_types, container):
        BaseFrame.__init__(self, feature_types)
        self.metadata.update(container.get_metadata_dict())
        self.metadata['system_type'] = 'container'


class SafeContainersCrawler(BaseCrawler):

    def __init__(self,
                 features=['os', 'cpu'],
                 environment='cloudsight',
                 user_list='ALL',
                 host_namespace='',
                 plugin_places=['plugins'],
                 frequency=-1,
                 options={}):

        BaseCrawler.__init__(
            self,
            features=features,
            plugin_places=plugin_places,
            options=options)
        plugins_manager.reload_env_plugin(environment, plugin_places)
        plugins_manager.reload_container_crawl_plugins(
            features, plugin_places, options)
        self.plugins = plugins_manager.get_container_crawl_plugins(features)
        self.environment = environment
        self.host_namespace = host_namespace
        self.user_list = user_list
        self.frequency = frequency

    
    def create_plugincont(self, guestcont):
        #TODO: build plugin cont image from Dockerfile first

        #plugincont_image = 'plugincont_image'
        #pip install docker=2.0.0          
        #client.containers.run("ruby", "tail -f /dev/null", pid_mode='container:d98cd4f1e518e671bc376ac429146937fbec9df7dbbfbb389e615a90c23ca27a', detach=True)
        # maybe userns_mode='host' 
        guestcont_id = guestcont.long_id
        guestcont_rootfs = utils.dockerutils.get_docker_container_rootfs_path(guestcont_id)
        plugincont_image = 'crawler_plugins12'
        plugincont = None
        seccomp_profile_path = os.getcwd() + '/utils/plugincont/seccomp-no-ptrace.json'
        client = docker.from_env()          
        try:
            plugincont = client.containers.run(
                image=plugincont_image, 
                name='plugin_cont',
                user='user1',
                command="/usr/bin/python2.7 crawler/crawler_lite.py --frequency="+frequency,
                pid_mode='container:'+guestcont_id,
                network_mode='container:'+guestcont_id,
                cap_add=["SYS_PTRACE","DAC_READ_SEARCH"],
                security_opt=['seccomp='+seccomp_profile_path],
                volumes={guestcont_rootfs:{'bind':'/rootfs_local','mode':'ro'}},
                detach=True)
        except:      
            print sys.exc_info()[0]
        guestcont.plugincont = plugincont

    def _add_iptable_rules(self):
        # pip install python-iptables
        rule = iptc.Rule()
        rule.protocol = "all"
        match = iptc.Match(rule, "owner")
        match.uid_owner = "166536"  #uid of plugin cont's user1 on host; from  dokcer userns remapping
        rule.add_match(match)
        rule.target = iptc.Target(rule, "DROP")
        chain = iptc.Chain(iptc.Table(iptc.Table.FILTER), "OUTPUT")
        chain.insert_rule(rule)
        #TODO

    def _setup_netcls_cgroup(self, plugincont_id):
        try:
            cgroup_netcls_path = '/sys/fs/cgroup/net_cls/docker/'+plugincont_id
            tasks_path = cgroup_netcls_path+'/tasks'
            block_path = cgroup_netcls_path+'/block'
            block_classid_path = block_path+'/net_cls.classid'
            block_tasks_path = block_path+'/tasks'
            
            if not os.path.isdir(block_path):
                os.makedirs(block_path)
            
            fd = open(block_classid_path,'w')
            fd.write('43')  #random cgroup net cls id
            fd.close()
            
            fd = open(tasks_path,'r')
            plugincont_pids = fd.readlines()  #should be just one pid == plugincont_pid
            fd.close()
            
            fd = open(block_tasks_path,'r')
            for pid in plugincont_pids:
                fd.write(pid)
            fd.close()
        except:      
            print sys.exc_info()[0]
        
    def set_plugincont_iptables(self, plugincont_id):
        try:
            client = docker.APIClient(base_url='unix://var/run/docker.sock')          
            plugincont_pid = client.inspect_container(plugincont_id)['State']['Pid']     
            #netns_path = '/var/run/netns'
            #if not os.path.isdir(netns_path):
            #    os.makedirs(netns_path)
            self._setup_netcls_cgroup(plugincont_id)
            run_as_another_namespace(plugincont_pid,
                                     ['net'],
                                     self._add_iptable_rules)

        except:      
            print sys.exc_info()[0]
    
    
    def setup_plugincont(self, guestcont):
        self.create_plugincont(guestcont)
        if guestcont.plugincont is not None:
            plugincont_id = guestcont.plugincont.id 
            self.set_plugincont_iptables(plugincont_id)
            # TODO:

    # Return list of features after reading frame from plugin cont
    def get_plugincont_features(self, guestcont):
        features = []
        if guestcont.plugincont is None:
            self.setup_plugincont(guestcont)
            if guestcont.plugincont is None:
                return features
            
        plugincont_id = guestcont.plugincont.id
        rootfs = utils.dockerutils.get_docker_container_rootfs_path(plugincont_id)
        frame_dir = rootfs+'/home/user1/'
        frame_list = os.listdir(frame_dir)
        frame_list.sort(key=int)
        
        if frame_list != []:
            try:
                earliest_frame_file = frame_dir+frame_list[0]
                fd = open(earliest_frame_file)
                for feature_line in fd.readlines():
                    (type, key, val) = feature_line.strip().split()
                    features.append((key, ast.literal_eval(val), type))
                fd.close()    
                os.remove(earliest_frame_file)
            except:
                print sys.exc_info()[0]
        
        return features
            

    def crawl_container(self, container, ignore_plugin_exception=True):
        """
        Crawls a specific container and returns a Frame for it.

        :param container: a Container object
        :param ignore_plugin_exception: just ignore exceptions in a plugin
        :return: a Frame object. The returned frame can have 0 features and
        still have metadata. This can occur if there were no plugins, or all
        the plugins raised an exception (and ignore_plugin_exception was True).
        """
        frame = ContainerFrame(self.features, container)

        # collect plugin crawl output for privileged plugins run at host
        for (plugin_obj, plugin_args) in self.plugins:
            try:
                frame.add_features(
                    plugin_obj.crawl(
                        container_id=container.long_id,
                        **plugin_args))
            except Exception as exc:
                if not ignore_plugin_exception:
                    raise exc

        # collect plugin crawl output from inside plugin sidecar container
        try:
            frame.add_features(self.get_plugincont_features(container))
        except Exception as exc:
            if not ignore_plugin_exception:
                raise exc

        return frame

    def polling_crawl(self, timeout, ignore_plugin_exception=True):
        """
        Crawls any container created before `timeout` seconds have elapsed.

        :param timeout: seconds to wait for new containers
        :param ignore_plugin_exception: just ignore exceptions in a plugin
        :return: a Frame object
        """
        # Not implemented
        sleep(timeout)      
        return None

    def crawl(self, ignore_plugin_exception=True):
        """
        Crawls all containers.

        :param ignore_plugin_exception: just ignore exceptions in a plugin
        :return: a list generator of Frame objects
        """
        containers_list = get_containers(
            user_list=self.user_list,
            host_namespace=self.host_namespace)
        for container in containers_list:
            yield self.crawl_container(container, ignore_plugin_exception)

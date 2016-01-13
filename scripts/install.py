#!/usr/bin/env python

# TODO: add installer support for membasez

import getopt
import copy
import logging
import os
import sys
from threading import Thread
from datetime import datetime
import socket
import Queue

sys.path = [".", "lib"] + sys.path
import testconstants
import time
from builds.build_query import BuildQuery
import logging.config
from membase.api.exception import ServerUnavailableException
from membase.api.rest_client import RestConnection, RestHelper
from remote.remote_util import RemoteMachineShellConnection, RemoteUtilHelper
from membase.helper.cluster_helper import ClusterOperationHelper
from testconstants import MV_LATESTBUILD_REPO
from testconstants import SHERLOCK_BUILD_REPO
from testconstants import COUCHBASE_REPO
from testconstants import CB_REPO
from testconstants import COUCHBASE_VERSION_2
from testconstants import COUCHBASE_VERSION_3
from testconstants import CB_VERSION_NAME
import TestInput


logging.config.fileConfig("scripts.logging.conf")
log = logging.getLogger()

def usage(err=None):
    print """\
Syntax: install.py [options]

Options:
 -p <key=val,...> Comma-separated key=value info.
 -i <file>        Path to .ini file containing cluster information.

Available keys:
 product=cb|mb           Used to specify couchbase or membase.
 version=SHORT_VERSION   Example: "2.0.0r-71".
 parallel=false          Useful when you're installing a cluster.
 toy=                    Install a toy build
 init_nodes=False        Initialize nodes
 vbuckets=               The number of vbuckets in the server installation.
 sync_threads=True       Sync or acync threads(+S or +A)
 erlang_threads=         Number of erlang threads (default=16:16 for +S type)
 upr=True                Enable UPR replication
 xdcr_upr=               Enable UPR for XDCR (temporary param until XDCR with UPR is stable), values: None | True | False


Examples:
 install.py -i /tmp/ubuntu.ini -p product=cb,version=2.2.0-792
 install.py -i /tmp/ubuntu.ini -p product=cb,version=2.2.0-792,url=http://builds.hq.northscale.net/latestbuilds....
 install.py -i /tmp/ubuntu.ini -p product=mb,version=1.7.1r-38,parallel=true,toy=keith
 install.py -i /tmp/ubuntu.ini -p product=mongo,version=2.0.2
 install.py -i /tmp/ubuntu.ini -p product=cb,version=0.0.0-704,toy=couchstore,parallel=true,vbuckets=1024

 # to run with build with require openssl version 1.0.0
 install.py -i /tmp/ubuntu.ini -p product=cb,version=2.2.0-792,openssl=1
"""
    sys.exit(err)


product = "membase-server(ms),couchbase-single-server(css),couchbase-server(cs),zynga(z)"

errors = {"UNREACHABLE": "",
          "UNINSTALL-FAILED": "unable to uninstall the product",
          "INSTALL-FAILED": "unable to install",
          "BUILD-NOT-FOUND": "unable to find build",
          "INVALID-PARAMS": "invalid params given"}


def installer_factory(params):
    if params.get("product", None) is None:
        sys.exit("ERROR: don't know what product you want installed")

    mb_alias = ["membase", "membase-server", "mbs", "mb"]
    cb_alias = ["couchbase", "couchbase-server", "cb"]
    sdk_alias = ["python-sdk", "pysdk"]
    es_alias = ["elasticsearch"]
    css_alias = ["couchbase-single", "couchbase-single-server", "css"]
    mongo_alias = ["mongo"]
    moxi_alias = ["moxi", "moxi-server"]

    if params["product"] in mb_alias:
        return MembaseServerInstaller()
    elif params["product"] in cb_alias:
        return CouchbaseServerInstaller()
    elif params["product"] in mongo_alias:
        return MongoInstaller()
    elif params["product"] in moxi_alias:
        return MoxiInstaller()
    elif params["product"] in sdk_alias:
        return SDKInstaller()
    elif params["product"] in es_alias:
        return ESInstaller()

    sys.exit("ERROR: don't know about product " + params["product"])


class Installer(object):

    def install(self, params):
        pass

    def initialize(self, params):
        pass

    def uninstall(self, params):
        remote_client = RemoteMachineShellConnection(params["server"])
        #remote_client.membase_uninstall()
        remote_client.couchbase_uninstall()
        remote_client.disconnect()

    def build_url(self, params):
        _errors = []
        version = ''
        server = ''
        openssl = ''
        names = []
        url = ''
        direct_build_url = None

        # replace "v" with version
        # replace p with product
        tmp = {}
        for k in params:
            value = params[k]
            if k == "v":
                tmp["version"] = value
            elif k == "p":
                tmp["version"] = value
            else:
                tmp[k] = value
        params = tmp

        ok = True
        if not "version" in params and len(params["version"]) < 5:
            _errors.append(errors["INVALID-PARAMS"])
            ok = False
        else:
            version = params["version"]

        if ok:
            if not "product" in params:
                _errors.append(errors["INVALID-PARAMS"])
                ok = False
        if ok:
            if not "server" in params:
                _errors.append(errors["INVALID-PARAMS"])
                ok = False
            else:
                server = params["server"]

        if ok:
            if "toy" in params:
                toy = params["toy"]
            else:
                toy = ""

        if ok:
            if "openssl" in params:
                openssl = params["openssl"]

        if ok:
            if "url" in params and params["url"] != "":
                direct_build_url = params["url"]
        if ok:
            mb_alias = ["membase", "membase-server", "mbs", "mb"]
            cb_alias = ["couchbase", "couchbase-server", "cb"]
            css_alias = ["couchbase-single", "couchbase-single-server", "css"]
            moxi_alias = ["moxi", "moxi-server"]

            if params["product"] in mb_alias:
                names = ['membase-server-enterprise', 'membase-server-community']
            elif params["product"] in cb_alias:
                if "type" in params and params["type"].lower() in "couchbase-server-community":
                    names = ['couchbase-server-community']
                elif "type" in params and params["type"].lower() in "couchbase-server-enterprise":
                    names = ['couchbase-server-enterprise']
                else:
                    names = ['couchbase-server-enterprise', 'couchbase-server-community']
            elif params["product"] in css_alias:
                names = ['couchbase-single-server-enterprise', 'couchbase-single-server-community']
            elif params["product"] in moxi_alias:
                names = ['moxi-server']
            else:
                ok = False
                _errors.append(errors["INVALID-PARAMS"])
            if "1" in openssl:
                names = ['couchbase-server-enterprise_centos6', 'couchbase-server-community_centos6', \
                         'couchbase-server-enterprise_ubuntu_1204', 'couchbase-server-community_ubuntu_1204']
            if "toy" in params:
                names = ['couchbase-server-enterprise']

        remote_client = RemoteMachineShellConnection(server)
        info = remote_client.extract_remote_info()
        remote_client.disconnect()
        if ok:
            timeout = 300
            if "timeout" in params:
                timeout = int(params["timeout"])
            releases_version = ["1.6.5.4", "1.7.0", "1.7.1", "1.7.1.1", "1.8.0"]
            cb_releases_version = ["1.8.1", "2.0.0", "2.0.1", "2.1.0", "2.1.1", "2.2.0",
                                    "2.5.0", "2.5.1", "2.5.2", "3.0.0", "3.0.1", "3.0.2",
                                    "3.0.3", "3.1.0", "3.1.1", "3.1.2"]
            build_repo = MV_LATESTBUILD_REPO
            if toy is not "":
                build_repo = CB_REPO
            elif version[:5] not in COUCHBASE_VERSION_2 and \
                 version[:5] not in COUCHBASE_VERSION_3:
                if version[:3] in CB_VERSION_NAME:
                    build_repo = CB_REPO + CB_VERSION_NAME[version[:3]] + "/"
                else:
                    sys.exit("version is not support yet")
            for name in names:
                if version[:5] in releases_version:
                    build = BuildQuery().find_membase_release_build(deliverable_type=info.deliverable_type,
                                                                     os_architecture=info.architecture_type,
                                                                     build_version=version,
                                                                     product='membase-server-enterprise')
                elif version[:5] in cb_releases_version:
                    build = BuildQuery().find_couchbase_release_build(
                                            deliverable_type=info.deliverable_type,
                                            os_architecture=info.architecture_type,
                                            build_version=version,
                                            product=name,
                                            os_version = info.distribution_version)
                else:
                    builds, changes = BuildQuery().get_all_builds(version=version, timeout=timeout, \
                                      direct_build_url=direct_build_url, \
                                      deliverable_type=info.deliverable_type, \
                                      architecture_type=info.architecture_type, \
                                      edition_type=name, \
                                      repo=build_repo, toy=toy, \
                                      distribution_version=info.distribution_version.lower(), \
                                      distribution_type=info.distribution_type.lower())
                    build = BuildQuery().find_build(builds, name, info.deliverable_type, \
                                                    info.architecture_type, version, toy=toy, \
                                                    openssl=openssl, direct_build_url=direct_build_url, \
                                                    distribution_version=info.distribution_version.lower(), \
                                                    distribution_type=info.distribution_type.lower())

                if build:
                    if 'amazon' in params:
                        type = info.type.lower()
                        if type == 'windows' and version in releases_version:
                            build.url = build.url.replace("http://builds.hq.northscale.net",
                                                          "https://s3.amazonaws.com/packages.couchbase")
                            build.url = build.url.replace("enterprise", "community")
                            build.name = build.name.replace("enterprise", "community")
                        else:
                            """ since url in S3 insert version into it, we need to put version
                                in like ..latestbuilds/3.0.0/... """
                            cb_version = version[:5]
                            build.url = build.url.replace("http://builds.hq.northscale.net/latestbuilds",
                                        "http://packages.northscale.com/latestbuilds/{0}".format(cb_version))
                            """ test enterprise version """
                            #build.url = build.url.replace("enterprise", "community")
                            #build.name = build.name.replace("enterprise", "community")
                    """ check if URL is live """
                    remote_client = RemoteMachineShellConnection(server)
                    if remote_client.is_url_live(build.url):
                        return build
                    else:
                        sys.exit("ERROR: URL is not good. Check URL again")
            _errors.append(errors["BUILD-NOT-FOUND"])
        msg = "unable to find a build for product {0} version {1} for package_type {2}"
        raise Exception(msg.format(names, version, info.deliverable_type))

    def is_socket_active(self, host, port, timeout=300):
        """ Check if remote socket is open and active

        Keyword arguments:
        host -- remote address
        port -- remote port
        timeout -- check timeout (in seconds)

        Returns:
        True -- socket is active
        False -- otherwise

        """
        start_time = time.time()

        sckt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        while time.time() - start_time < timeout:
            try:
                sckt.connect((host, port))
                sckt.shutdown(2)
                sckt.close()
                return True
            except:
                time.sleep(10)

        return False

class MembaseServerInstaller(Installer):
    def __init__(self):
        Installer.__init__(self)

    def initialize(self, params):
        start_time = time.time()
        cluster_initialized = False
        server = params["server"]
        while time.time() < (start_time + (5 * 60)):
            rest = RestConnection(server)
            try:
                if server.data_path:
                    remote_client = RemoteMachineShellConnection(server)
                    remote_client.execute_command('rm -rf {0}/*'.format(server.data_path))
                    # Make sure that data_path is writable by membase user
                    remote_client.execute_command("chown -R membase.membase {0}".format(server.data_path))
                    remote_client.disconnect()
                    rest.set_data_path(data_path=server.data_path)
                rest.init_cluster(username=server.rest_username, password=server.rest_password)
                rest.init_cluster_memoryQuota(memoryQuota=rest.get_nodes_self().mcdMemoryReserved)
                cluster_initialized = True
                break
            except ServerUnavailableException:
                log.error("error happened while initializing the cluster @ {0}".format(server.ip))
            log.info('sleep for 5 seconds before trying again ...')
            time.sleep(5)
        if not cluster_initialized:
            raise Exception("unable to initialize membase node")

    def install(self, params, queue=None):
        try:
            build = self.build_url(params)
        except Exception, e:
            if queue:
                queue.put(False)
            raise e
        remote_client = RemoteMachineShellConnection(params["server"])
        info = remote_client.extract_remote_info()
        type = info.type.lower()
        server = params["server"]
        if "vbuckets" in params:
            vbuckets = int(params["vbuckets"][0])
        else:
            vbuckets = None
        if "swappiness" in params:
            swappiness = int(params["swappiness"])
        else:
            swappiness = 0

        if "openssl" in params:
            openssl = params["openssl"]
        else:
            openssl = ""

        if type == "windows":
            build = self.build_url(params)
            remote_client.download_binary_in_win(build.url, params["version"])
            success = remote_client.install_server_win(build, params["version"], \
                                                       vbuckets=vbuckets)
        else:
            downloaded = remote_client.download_build(build)
            if not downloaded:
                log.error('server {1} unable to download binaries : {0}' \
                          .format(build.url, params["server"].ip))
                return False
            path = server.data_path or '/tmp'
            success &= remote_client.install_server(build, path=path, vbuckets=vbuckets, \
                                                    swappiness=swappiness, openssl=openssl)
            ready = RestHelper(RestConnection(params["server"])).is_ns_server_running(60)
            if not ready:
                log.error("membase-server did not start...")
            log.info('wait 5 seconds for membase server to start')
            time.sleep(5)
        remote_client.disconnect()
        if queue:
            queue.put(success)
        return success


class CouchbaseServerInstaller(Installer):
    def __init__(self):
        Installer.__init__(self)

    def initialize(self, params):
        start_time = time.time()
        cluster_initialized = False
        server = params["server"]
        remote_client = RemoteMachineShellConnection(params["server"])
        while time.time() < start_time + 5 * 60:
            try:
                rest = RestConnection(server)

                # Optionally change node name and restart server
                if params.get('use_domain_names', 0):
                    RemoteUtilHelper.use_hostname_for_server_settings(server)

                # Make sure that data_path and index_path are writable by couchbase user
                for path in set(filter(None, [server.data_path, server.index_path])):
                    time.sleep(3)

                    for cmd in ("rm -rf {0}/*".format(path),
                                "chown -R couchbase:couchbase {0}".format(path)):
                        remote_client.execute_command(cmd)
                rest.set_data_path(data_path=server.data_path,
                                       index_path=server.index_path)
                time.sleep(3)

                # Initialize cluster
                if "init_nodes" in params:
                    init_nodes = params["init_nodes"]
                else:
                    init_nodes = "True"
                if (isinstance(init_nodes, bool) and init_nodes) or (init_nodes.lower() == "true"):
                    if server.services:
                        rest.init_node_services(username=server.rest_username,
                                                password=server.rest_password,
                                                services=server.services.split(','))
                    rest.init_cluster(username=server.rest_username,
                                      password=server.rest_password)
                    memory_quota = rest.get_nodes_self().mcdMemoryReserved
                    # give the cluster time to get the memQuota
                    while memory_quota == 0:
                       time.sleep(1)
                       memory_quota = rest.get_nodes_self().mcdMemoryReserved

                    rest.init_cluster_memoryQuota(memoryQuota=memory_quota)

                # TODO: Symlink data-dir to custom path
                # remote_client.stop_couchbase()
                # remote_client.execute_command('mv /opt/couchbase/var {0}'.format(server.data_path))
                # remote_client.execute_command('ln -s {0}/var /opt/couchbase/var'.format(server.data_path))
                # remote_client.execute_command("chown -h couchbase:couchbase /opt/couchbase/var")
                # remote_client.start_couchbase()

                # Optionally disable consistency check
                if params.get('disable_consistency', 0):
                    rest.set_couchdb_option(section='couchdb',
                                            option='consistency_check_ratio',
                                            value='0.0')

                # memcached env variable
                mem_req_tap_env = params.get('MEMCACHED_REQS_TAP_EVENT', 0)
                if mem_req_tap_env:
                    remote_client.set_environment_variable('MEMCACHED_REQS_TAP_EVENT',
                                                           mem_req_tap_env)
                remote_client.disconnect()
                # TODO: Make it work with windows
                if "erlang_threads" in params:
                    num_threads = params.get('erlang_threads', testconstants.NUM_ERLANG_THREADS)
                    # Stop couchbase-server
                    ClusterOperationHelper.stop_cluster([server])
                    if "sync_threads" in params or ':' in num_threads:
                        sync_threads = params.get('sync_threads', True)
                    else:
                        sync_threads = False
                    # Change type of threads(sync/async) and num erlang threads
                    ClusterOperationHelper.change_erlang_threads_values([server], sync_threads, num_threads)
                    # Start couchbase-server
                    ClusterOperationHelper.start_cluster([server])
                if "erlang_gc_level" in params:
                    erlang_gc_level = params.get('erlang_gc_level', None)
                    if erlang_gc_level is None:
                        # Don't change the value
                        break
                    # Stop couchbase-server
                    ClusterOperationHelper.stop_cluster([server])
                    # Change num erlang threads
                    ClusterOperationHelper.change_erlang_gc([server], erlang_gc_level)
                    # Start couchbase-server
                    ClusterOperationHelper.start_cluster([server])
                cluster_initialized = True
                break
            except ServerUnavailableException:
                log.error("error happened while initializing the cluster @ {0}".format(server.ip))
            log.info('sleep for 5 seconds before trying again ...')
            time.sleep(5)
        if not cluster_initialized:
            sys.exit("unable to initialize couchbase node")

    def install(self, params, queue=None):
        try:
            build = self.build_url(params)
        except Exception, e:
            if queue:
                queue.put(False)
            raise e
        remote_client = RemoteMachineShellConnection(params["server"])
        info = remote_client.extract_remote_info()
        type = info.type.lower()
        server = params["server"]
        if "swappiness" in params:
            swappiness = int(params["swappiness"])
        else:
            swappiness = 0
        if "openssl" in params:
            openssl = params["openssl"]
        else:
            openssl = ""

        if "vbuckets" in params:
            vbuckets = int(params["vbuckets"][0])
        else:
            vbuckets = None

        if "upr" in params and params["upr"].lower() != "none":
            upr = params["upr"].lower() == 'true'
        else:
            upr = None

        if "xdcr_upr" not in params:
            xdcr_upr = None
        else:
            xdcr_upr = eval(params["xdcr_upr"].capitalize())

        if type == "windows":
            remote_client.download_binary_in_win(build.url, params["version"])
            success = remote_client.install_server_win(build, \
                        params["version"].replace("-rel", ""), vbuckets=vbuckets)
        else:
            downloaded = remote_client.download_build(build)
            if not downloaded:
                log.error('server {1} unable to download binaries : {0}' \
                          .format(build.url, params["server"].ip))
                return False
            # TODO: need separate methods in remote_util for couchbase and membase install
            path = server.data_path or '/tmp'
            try:
                success = remote_client.install_server(build, path=path, vbuckets=vbuckets, swappiness=swappiness,
                                                       openssl=openssl, upr=upr, xdcr_upr=xdcr_upr)
                log.info('wait 5 seconds for membase server to start')
                time.sleep(5)
                if "rest_vbuckets" in params:
                    rest_vbuckets = int(params["rest_vbuckets"])
                    ClusterOperationHelper.set_vbuckets(server, rest_vbuckets)
            except BaseException, e:
                success = False
                log.error("installation failed: {0}".format(e))
        remote_client.disconnect()
        if queue:
            queue.put(success)
        return success


class MongoInstaller(Installer):
    def get_server(self, params):
        version = params["version"]
        server = params["server"]
        server.product_name = "mongodb-linux-x86_64-" + version
        server.product_tgz = server.product_name + ".tgz"
        server.product_url = "http://fastdl.mongodb.org/linux/" + server.product_tgz
        return server

    def mk_remote_client(self, server):
        remote_client = RemoteMachineShellConnection(server)

        info = remote_client.extract_remote_info()
        type = info.type.lower()
        if type == "windows":
            sys.exit("ERROR: please teach me about windows one day.")

        return remote_client

    def uninstall(self, params):
        server = self.get_server(params)
        remote_client = self.mk_remote_client(server)
        remote_client.execute_command("killall mongod mongos")
        remote_client.execute_command("killall -9 mongod mongos")
        remote_client.execute_command("rm -rf ./{0}".format(server.product_name))

    def install(self, params):
        server = self.get_server(params)
        remote_client = self.mk_remote_client(server)

        downloaded = remote_client.download_binary(server.product_url, "tgz", server.product_tgz)
        if not downloaded:
            log.error(downloaded, 'server {1} unable to download binaries : {0}' \
                      .format(server.product_url, server.ip))

        remote_client.execute_command("tar -xzvf /tmp/{0}".format(server.product_tgz))

    def initialize(self, params):
        server = self.get_server(params)
        remote_client = self.mk_remote_client(server)
        remote_client.execute_command("mkdir -p {0}/data/data-27019 {0}/data/data-27018 {0}/log". \
                                          format(server.product_name))
        remote_client.execute_command("./{0}/bin/mongod --port 27019 --fork --rest --configsvr" \
                                          " --logpath ./{0}/log/mongod-27019.out" \
                                          " --dbpath ./{0}/data/data-27019". \
                                          format(server.product_name))
        remote_client.execute_command("./{0}/bin/mongod --port 27018 --fork --rest --shardsvr" \
                                          " --logpath ./{0}/log/mongod-27018.out" \
                                          " --dbpath ./{0}/data/data-27018". \
                                          format(server.product_name))

        log.info("check that config server started before launching mongos")
        if self.is_socket_active(host=server.ip, port=27019):
            remote_client.execute_command(("./{0}/bin/mongos --port 27017 --fork" \
                                           " --logpath ./{0}/log/mongos-27017.out" \
                                           " --configdb " + server.ip + ":27019"). \
                                          format(server.product_name))
        else:
            log.error("Connection with MongoDB config server was not established.")
            sys.exit()

class MoxiInstaller(Installer):
    def __init__(self):
        Installer.__init__(self)

    def initialize(self, params):
        log.info('There is no initialize phase for moxi')

    def uninstall(self, params):
        remote_client = RemoteMachineShellConnection(params["server"])
        remote_client.membase_uninstall()
        remote_client.couchbase_uninstall()
        remote_client.moxi_uninstall()
        remote_client.disconnect()

    def install(self, params, queue=None):
        try:
            build = self.build_url(params)
        except Exception, e:
            if queue:
                queue.put(False)
            raise e
        remote_client = RemoteMachineShellConnection(params["server"])
        info = remote_client.extract_remote_info()
        type = info.type.lower()
        if type == "windows":
            raise Exception("Not implemented for windows")
        else:
            downloaded = remote_client.download_build(build)
            if not downloaded:
                log.error('server {1} unable to download binaries : {0}' \
                          .format(build.url, params["server"].ip))
                return False
            try:
                success = remote_client.install_moxi(build)
            except BaseException, e:
                success = False
                log.error("installation failed: {0}".format(e))
        remote_client.disconnect()
        if queue:
            queue.put(success)
        return success

class SDKInstaller(Installer):
    def __init__(self):
        pass

    def initialize(self, params):
        log.info('There is no initialize phase for sdk installation')

    def uninstall(self):
        pass

    def install(self, params):
        remote_client = RemoteMachineShellConnection(params["server"])
        info = remote_client.extract_remote_info()
        os = info.type.lower()
        type = info.deliverable_type.lower()
        version = info.distribution_version.lower()
        if params['subdoc'] == 'True':
            sdk_url = 'git+git://github.com/mnunberg/couchbase-python-client.git@subdoc'
        else:
            sdk_url = 'git+git://github.com/couchbase/couchbase-python-client.git'
        if os == 'linux':
            if (type == 'rpm' and params['subdoc'] == 'False'):
                repo_file = '/etc/yum.repos.d/couchbase.repo'
                baseurl = ''
                if (version.find('centos') != -1 and version.find('6.2') != -1):
                    baseurl = 'http://packages.couchbase.com/rpm/6.2/x86-64'
                elif (version.find('centos') != -1 and version.find('6.4') != -1):
                    baseurl = 'http://packages.couchbase.com/rpm/6.4/x86-64'
                elif (version.find('centos') != -1 and version.find('7') != -1):
                    baseurl = 'http://packages.couchbase.com/rpm/7/x86_64'
                else:
                    log.info("os version {0} not supported".format(version))
                    exit(1)
                remote_client.execute_command("rm -rf {0}".format(repo_file))
                remote_client.execute_command("touch {0}".format(repo_file))
                remote_client.execute_command("echo [couchbase] >> {0}".format(repo_file))
                remote_client.execute_command("echo enabled=1 >> {0}".format(repo_file))
                remote_client.execute_command("echo name = Couchbase package repository \
                        >> {0}".format(repo_file))
                remote_client.execute_command("echo baseurl = {0} >> \
                        {1}".format(baseurl, repo_file))
                remote_client.execute_command("yum -n update")
                remote_client.execute_command("yum -y install \
                        libcouchbase2-libevent libcouchbase-devel libcouchbase2-bin")
                remote_client.execute_command("yum -y install python-pip")
                remote_client.execute_command("pip -y uninstall couchbase")
                remote_client.execute_command("pip -y install {0}".format(sdk_url))

            elif (type == 'rpm' and params['subdoc'] == 'True'):
                package_url = ''
                lcb_core = ''
                lcb_libevent  = ''
                lcb_devel = ''
                lcb_bin = ''

                if (version.find('centos') != -1 and version.find('6') != -1):
                    package_url = 'http://172.23.105.153/228/DIST/el6/'
                    lcb_core =  'libcouchbase2-core-2.5.4-11.r10ga37efd8.SP.el6.x86_64.rpm'
                    lcb_libevent = 'libcouchbase2-libevent-2.5.4-11.r10ga37efd8.SP.el6.x86_64.rpm'
                    lcb_devel = 'libcouchbase-devel-2.5.4-11.r10ga37efd8.SP.el6.x86_64.rpm'
                    lcb_bin = 'libcouchbase2-bin-2.5.4-11.r10ga37efd8.SP.el6.x86_64.rpm'
                    remote_client.execute_command('rpm -ivh http://dl.fedoraproject.org/pub/epel/6/x86_64/epel-release-6-8.noarch.rpm')

                elif (version.find('centos') != -1 and version.find('7') != -1):
                    package_url = 'http://172.23.105.153/228/DIST/el7/'
                    lcb_core = 'libcouchbase2-core-2.5.4-11.r10ga37efd8.SP.el7.centos.x86_64.rpm'
                    lcb_libevent = 'libcouchbase2-libevent-2.5.4-11.r10ga37efd8.SP.el7.centos.x86_64.rpm'
                    lcb_devel = 'libcouchbase-devel-2.5.4-11.r10ga37efd8.SP.el7.centos.x86_64.rpm'
                    lcb_bin = 'libcouchbase2-bin-2.5.4-11.r10ga37efd8.SP.el7.centos.x86_64.rpm'

                    remote_client.execute_command('yum -y  install epel-release')

                remote_client.execute_command('yum -y remove "libcouchbase*"')
                remote_client.execute_command('rm -rf {0} {1} {2} {3}'.format(lcb_core,
                    lcb_libevent, lcb_devel, lcb_bin))
                remote_client.execute_command('wget {0}{1}'.format(package_url, lcb_core))
                remote_client.execute_command('wget {0}{1}'.format(package_url,
                    lcb_libevent))
                remote_client.execute_command('wget {0}{1}'.format(package_url,lcb_devel))
                remote_client.execute_command('wget {0}{1}'.format(package_url,lcb_bin))
                remote_client.execute_command('rpm -ivh {0} {1} {2}'.format(lcb_core,
                    lcb_libevent,lcb_devel,lcb_bin))
                remote_client.execute_command('yum -y install python-pip')
                remote_client.execute_command('pip -y uninstall couchbase')
                remote_client.execute_command('pip -y install {0}'.format(sdk_url))

            elif (type == "deb" and params['subdoc'] == 'False'):
                repo_file = "/etc/sources.list.d/couchbase.list"
                entry = ""
                if (version.find("ubuntu") != -1 and version.find("12.04") != -1):
                    entry = "http://packages.couchbase.com/ubuntu precise precise/main"
                elif (version.find("ubuntu") != -1 and version.find("14.04") != -1):
                    entry = "http://packages.couchbase.com/ubuntu trusty trusty/main"
                elif (version.find("debian") != -1 and version.find("7") != -1):
                    entry = "http://packages.couchbase.com/ubuntu wheezy wheezy/main"
                else:
                    log.info("os version {0} not supported".format(version))
                    exit(1)
                remote_client.execute_command("rm -rf {0}".format(repo_file))
                remote_client.execute_command("touch {0}".format(repo_file))
                remote_client.execute_command("deb {0} >> {1}".format(entry, repo_file))
                remote_client.execute_command("apt-get update")
                remote_client.execute_command("apt-get -y install libcouchbase2-libevent \
                        libcouchbase-devel libcouchbase2-bin")
                remote_client.execute_command("apt-get -y install pip")
                remote_client.execute_command("pip -y uninstall couchbase")
                remote_client.execute_command("pip -y install {0}".format(sdk_url))
        if os == "mac":
            remote_client.execute_command("brew install libcouchbase;\
                    brew link libcouchbase")
            remote_client.execute_command("brew install pip; brew link pip")
            remote_client.execute_command("pip install {0}".format(sdk_url))
        if os == "windows":
            log.info('Currently not supported')
        remote_client.disconnect()
        return True


class ESInstaller(object):
    def __init__(self):
       self.remote_client = None
       pass

    def initialize(self, params):
        self.remote_client.execute_command("~/elasticsearch/bin/elasticsearch > es.log 2>&1 &")

    def install(self, params):
        self.remote_client = RemoteMachineShellConnection(params["server"])
        self.remote_client.execute_command("pkill -f elasticsearch")
        self.remote_client.execute_command("rm -rf ~/elasticsearch")
        self.remote_client.execute_command("rm -rf ~/elasticsearch-*.tar.gz*")
        download_url = "https://download.elasticsearch.org/elasticsearch/elasticsearch/elasticsearch-{0}.tar.gz".format(params["version"])
        self.remote_client.execute_command("wget {0}".format(download_url))
        self.remote_client.execute_command("tar xvzf elasticsearch-{0}.tar.gz; mv elasticsearch-{0} elasticsearch".format(params["version"]))
        self.remote_client.execute_command("echo couchbase.password: password >> ~/elasticsearch/config/elasticsearch.yml")
        self.remote_client.execute_command("echo network.bind_host: _eth0:ipv4_ >> ~/elasticsearch/config/elasticsearch.yml")
        self.remote_client.execute_command("echo couchbase.port: 9091 >> ~/elasticsearch/config/elasticsearch.yml")
        self.remote_client.execute_command("~/elasticsearch/bin/plugin install {0}".format(params["plugin-url"]))
        self.remote_client.execute_command("~/elasticsearch/bin/plugin install https://github.com/mobz/elasticsearch-head/archive/master.zip")
        return True

    def __exit__(self):
        self.remote_client.disconnect()


class InstallerJob(object):
    def sequential_install(self, servers, params):
        installers = []

        for server in servers:
            _params = copy.deepcopy(params)
            _params["server"] = server
            installers.append((installer_factory(_params), _params))

        for installer, _params in installers:
            try:
                installer.uninstall(_params)
                if "product" in params and params["product"] in ["couchbase", "couchbase-server", "cb"]:
                    success = True
                    for server in servers:
                        success &= not RemoteMachineShellConnection(server).is_couchbase_installed()
                    if not success:
                        print "thread {0} finished. Server:{1}.Couchbase is still" + \
                              " installed after uninstall".format(t.name, server)
                        return success
                print "uninstall succeeded"
            except Exception as ex:
                print "unable to complete the uninstallation: ", ex
        success = True
        for installer, _params in installers:
            try:
                success &= installer.install(_params)
                try:
                    installer.initialize(_params)
                except Exception as ex:
                    print "unable to initialize the server after successful installation", ex
            except Exception as ex:
                print "unable to complete the installation: ", ex
        return success

    def parallel_install(self, servers, params):
        uninstall_threads = []
        install_threads = []
        initializer_threads = []
        queue = Queue.Queue()
        success = True
        for server in servers:
            _params = copy.deepcopy(params)
            _params["server"] = server
            u_t = Thread(target=installer_factory(params).uninstall,
                       name="uninstaller-thread-{0}".format(server.ip),
                       args=(_params,))
            i_t = Thread(target=installer_factory(params).install,
                       name="installer-thread-{0}".format(server.ip),
                       args=(_params, queue))
            init_t = Thread(target=installer_factory(params).initialize,
                       name="initializer-thread-{0}".format(server.ip),
                       args=(_params,))
            uninstall_threads.append(u_t)
            install_threads.append(i_t)
            initializer_threads.append(init_t)
        for t in uninstall_threads:
            t.start()
        for t in uninstall_threads:
            t.join()
            print "thread {0} finished".format(t.name)
        if "product" in params and params["product"] in ["couchbase", "couchbase-server", "cb"]:
            success = True
            for server in servers:
                success &= not RemoteMachineShellConnection(server).is_couchbase_installed()
            if not success:
                print "Server:{0}.Couchbase is still installed after uninstall".format(server)
                return success
        for t in install_threads:
            t.start()
        for t in install_threads:
            t.join()
            print "thread {0} finished".format(t.name)
        while not queue.empty():
            success &= queue.get()
        if not success:
            print "installation failed. initializer threads were skipped"
            return success
        for t in initializer_threads:
            t.start()
        for t in initializer_threads:
            t.join()
            print "thread {0} finished".format(t.name)
        """ remove any capture files left after install windows """
        remote_client = RemoteMachineShellConnection(servers[0])
        type = remote_client.extract_remote_info().distribution_type
        remote_client.disconnect()
        if type.lower() == 'windows':
            for server in servers:
                shell = RemoteMachineShellConnection(server)
                shell.execute_command("rm -f /cygdrive/c/automation/*_172.23*")
                shell.execute_command("rm -f /cygdrive/c/automation/*_10.17*")
                os.system("rm -f resources/windows/automation/*_172.23*")
                os.system("rm -f resources/windows/automation/*_10.17*")
        return success


def check_build(input):
        _params = copy.deepcopy(input.test_params)
        _params["server"] = input.servers[0]
        installer = installer_factory(_params)
        try:
            build = installer.build_url(_params)
            log.info("Found build: {0}".format(build))
        except Exception:
            log.error("Cannot find build {0}".format(_params))
            exit(1)

params = {"ini": "resources/jenkins/fusion.ini",
          "product": "ms", "version": "1.7.1r-31", "amazon": "false"}

def main():
    log_install_failed = "some nodes were not install successfully!"
    try:
        (opts, args) = getopt.getopt(sys.argv[1:], 'hi:p:', [])
        for o, a in opts:
            if o == "-h":
                usage()

        if len(sys.argv) <= 1:
            usage()

        input = TestInput.TestInputParser.get_test_input(sys.argv)
        if not input.servers:
            usage("ERROR: no servers specified. Please use the -i parameter.")
    except IndexError:
        usage()
    except getopt.GetoptError, err:
        usage("ERROR: " + str(err))
    # TODO: This is not broken, but could be something better
    #      like a validator, to check SSH, input params etc
    # check_build(input)

    if "parallel" in input.test_params:
        # workaround for a python2.6 bug of using strptime with threads
        datetime.strptime("30 Nov 00", "%d %b %y")
        success = InstallerJob().parallel_install(input.servers, input.test_params)
    else:
        success = InstallerJob().sequential_install(input.servers, input.test_params)
    if not success:
        sys.exit(log_install_failed)
    if "product" in input.test_params and input.test_params["product"] in ["couchbase", "couchbase-server", "cb"]:
        print "verify installation..."
        success = True
        for server in input.servers:
            success &= RemoteMachineShellConnection(server).is_couchbase_installed()
        if not success:
            sys.exit(log_install_failed)
    if "product" in input.test_params and input.test_params["product"] in ["moxi", "moxi-server"]:
        print "verify installation..."
        success = True
        for server in input.servers:
            success &= RemoteMachineShellConnection(server).is_moxi_installed()
        if not success:
            sys.exit(log_install_failed)

if __name__ == "__main__":
    main()

from time import sleep
import logging
import os
import json
from kubernetes import client, utils
from kubernetes.stream import stream
from kubernetes.client.rest import ApiException

def assertEventually(checker, delay, count, inspector=None):
    ix = 0
    err = ""
    while (ix < count):
        err = checker()
        if err == "":
            return
        ix += 1
        sleep(delay)
    print("Error is: {}".format(err))
    if inspector is not None:
        inspector()
    assert False

class SafeDict(dict):
    'Provide a default value for missing keys'
    def __missing__(self, key):
        return 'missing'

def tcLog(descr):
    print("\n*** TEST CASE: {}\n".format(descr))

def inspectLog(descr):
    print("\n!!! ONFAIL: {}\n".format(descr))

def logSetup():
    supportedLogLevels = ['INFO', 'DEBUG', 'ERROR']
    logLevel = os.getenv('LOG_LEVEL', 'INFO')
    if logLevel == "":
        logLevel = 'INFO'

    if logLevel not in supportedLogLevels:
        print("Supported log levels are:{}".format(supportedLogLevels))
        print("Overriding to INFO\n")
        logLevel = 'INFO'

    logging.basicConfig(level=logLevel)

def checkPodDeleted(kapi, ns, name, timeout=30):
    def deleteChecker():
        resp = kapi.list_namespaced_pod(ns)
        for pod in resp.items:
            if pod.metadata.name == name:
                return "{} still present".format(name)

        return ""

    assertEventually(deleteChecker, 1, timeout)

def scaleRc(name, replicas, ns="default"):
    v1 = client.CoreV1Api()
    scale = v1.read_namespaced_replication_controller_scale(name, ns)
    scale.spec.replicas = replicas
    resp = v1.replace_namespaced_replication_controller_scale(name, ns, scale)
    rcCheckScale(name, replicas, ns)

def rcCheckScale(name, replicas, ns="default"):
    v1 = client.CoreV1Api()
    def scaleChecker():
        curr = v1.read_namespaced_replication_controller_status(name, ns)
        if curr.status.ready_replicas is None and replicas == 0:
            return ""

        if curr.status.ready_replicas == replicas:
            return ""

        return "expected {} replicas, got {}".format(replicas, curr.status.ready_replicas)

    assertEventually(scaleChecker, 1, 30)

def checkPodsRemoved(selector, ns="default"):
    v1 = client.CoreV1Api()
    def checker():
        resp = v1.list_namespaced_pod(ns, label_selector=selector)
        if len(resp.items) == 0:
            return ""

        return "{} pods still present".format(len(resp.items))

    assertEventually(checker, 1, 45)

def getPodIP(name, ns):
    v1 = client.CoreV1Api()
    resp = v1.read_namespaced_pod_status(name, ns)
    return resp.status.pod_ip

def getPodNodeIP(name, ns):
    v1 = client.CoreV1Api()
    resp = v1.read_namespaced_pod_status(name, ns)
    return resp.status.host_ip

def getNodeIPs(name, ns):
    result = dict()
    v1 = client.CoreV1Api()
    resp = v1.list_node()
    for node in resp.items:
        result[node.metadata.name] = node.status.addresses[0].address

    return result

def namespaceExists(ns):
    v1 = client.CoreV1Api()
    ns_list = v1.list_namespace()
    for ns_obj in ns_list.items:
        if ns_obj.metadata.name == ns:
            return True
    return False

def getSysNs():
    if namespaceExists("aci-containers-system"):
        return "aci-containers-system"

    return "kube-system"

def checkAgentLog():
    v1 = client.CoreV1Api()
    systemNs = getSysNs()
    pod_list = v1.list_namespaced_pod(systemNs, label_selector="name=aci-containers-host")
    for pod in pod_list.items:
        resp = v1.read_namespaced_pod_log(pod.metadata.name, systemNs, container="opflex-agent")
        #print("Checking agent log on {}".format(pod.metadata.name))
        #assert "Failed to get VirtualRouterIp" not in resp
        #assert "regular" not in resp

def verifyAgentEPs(epIPs):
    ret_str = ""
    systemNs = getSysNs()
    v1 = client.CoreV1Api()
    pod_list = v1.list_namespaced_pod(systemNs, label_selector="name=aci-containers-host")
    cmd = "gbp_inspect -w 100000 -fprq DmtreeRoot -t dump".split()
    for pod in pod_list.items:
        resp = stream(v1.connect_get_namespaced_pod_exec, pod.metadata.name, systemNs, container="opflex-agent", command=cmd, stderr=True, stdin=False, stdout=True, tty=False)
        for epIP in epIPs:
            if epIP not in resp:
                #print("{} not found on node {}".format(epIP, pod.status.pod_ip)
                ret_str = ret_str + "{}/{}".format(epIP, pod.status.pod_ip)

    return ret_str

def verifyAgentContracts(contracts, expect):
    ret_str = ""
    systemNs = getSysNs()
    v1 = client.CoreV1Api()
    pod_list = v1.list_namespaced_pod(systemNs, label_selector="name=aci-containers-host")
    cmd = "gbp_inspect -w 100000 -fprq DmtreeRoot -t dump".split()
    for pod in pod_list.items:
        resp = stream(v1.connect_get_namespaced_pod_exec, pod.metadata.name, systemNs, container="opflex-agent", command=cmd, stderr=True, stdin=False, stdout=True, tty=False)
        for c in contracts:
            if c not in resp and expect:
                ret_str = ret_str + "{} not/{}".format(c, pod.status.pod_ip)
            if c in resp and not expect:
                ret_str = ret_str + "{} on/{}".format(c, pod.status.pod_ip)

    return ret_str

def verifyPing(podName, ns, dest, expSuccess=True):
    pod_ip = getPodIP(podName, ns)
    print("ping: {} -> {}".format(pod_ip, dest))
    ping_cmd = ['ping', '-c', '3', '-W', '1', dest]
    v1 = client.CoreV1Api()
    def pingChecker():
        resp = stream(v1.connect_get_namespaced_pod_exec, podName, ns,
                              command=ping_cmd, stderr=True, stdin=False, stdout=True, tty=False)
        print("=>Resp is {}".format(resp))
        if expSuccess:
            if "3 packets received" not in resp:
                return "3 packets not received"
            return ""
        else:
            if "0 packets received" not in resp:
                return resp
            return ""
    assertEventually(pingChecker, 2, 30)

def verifyTCP(podName, ns, destIP, destPort, expSuccess=True):
    nc_cmd = ['nc', '-zvnw', '1', destIP, destPort]
    v1 = client.CoreV1Api()
    def ncChecker():
        resp = stream(v1.connect_get_namespaced_pod_exec, podName, ns,
                              command=nc_cmd, stderr=True, stdin=False, stdout=True, tty=False)
        if expSuccess:
            if "open" in resp:
                return ""
        else:
            if "timed out" in resp:
                return ""
        return "failed:" + resp
    assertEventually(ncChecker, 2, 30)

def deletePod(ns, name):
    v1 = client.CoreV1Api()
    v1.delete_namespaced_pod(name, ns, client.V1DeleteOptions())
    checkPodDeleted(v1, ns, name, 60) 

def getGwIP():
    ns = getSysNs()
    v1 = client.CoreV1Api()
    c_pods = []
    pod_list = v1.list_namespaced_pod(ns, label_selector="name=aci-containers-controller")
    for pod in pod_list.items:
        if pod.status.phase == "Running":
            c_pods.append(pod.metadata.name)

    gbp_cmd = ['cat', '"/usr/local/etc/aci-containers/gbp-server.conf"']
    for pod in c_pods:
        gbp_resp = stream(v1.connect_get_namespaced_pod_exec, pod, ns, command=gbp_cmd,
                          stderr=True, stdin=False, stdout=True, tty=False)
        for line in gbp_resp.splitlines():
            if "pod-subnet" in line:
                snet = line.split("\"")[3]
                return snet.split("/")[0]
    return ""
                          

def checkGwFlows(gwIP):
    gwIP = os.getenv('GW_IP', gwIP)
    print("gwIP: {}".format(gwIP))
    ns = getSysNs()
    v1 = client.CoreV1Api()
    ovs_pods = []
    pod_list = v1.list_namespaced_pod(ns, label_selector="name=aci-containers-openvswitch")
    for pod in pod_list.items:
        if pod.status.phase == "Running":
            ovs_pods.append(pod.metadata.name)
    ovs_cmd = "ovs-ofctl dump-flows br-int -OOpenFlow13 table=5".split(" ")
    def flowChecker():
        for ovs_pod in ovs_pods:
            ovs_resp = stream(v1.connect_get_namespaced_pod_exec, ovs_pod, ns,
                              command=ovs_cmd, stderr=True, stdin=False, stdout=True, tty=False)
            found = False
            for line in ovs_resp.splitlines():
                if gwIP in line:
                    found = True
            if not found:
                return "{} not found on {}".format(gwIP, ovs_pod)
        return ""
    assertEventually(flowChecker, 1, 30)

def scaleDep(ns, name, replicas):
    v1 = client.AppsV1Api()
    scale = v1.read_namespaced_deployment_scale(name, ns)
    scale.spec.replicas = replicas
    resp = v1.replace_namespaced_deployment_scale(name, ns, scale)
    def scaleChecker():
        curr = v1.read_namespaced_deployment_status(name, ns)
        if curr.status.ready_replicas is None and replicas == 0:
            return ""

        if curr.status.ready_replicas == replicas:
            return ""

        return "expected {} replicas, got {}".format(replicas, curr.status.ready_replicas)

    assertEventually(scaleChecker, 1, 30)

def createDs(name):
    v1 = client.CoreV1Api()
    k8s_client = client.ApiClient()
    try:
        utils.create_from_yaml(k8s_client, "yamls/{}.yaml".format(name))
    except ApiException as e:
        logging.debug("{} - ignored".format(e.reason))

    lSel = "name={}".format(name)
    def readyChecker():
        pod_list = v1.list_namespaced_pod("default", label_selector=lSel)
        if len(pod_list.items) < 2:
            return "Not enough pods"

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                return "pod {} status is {}".format(pod.metadata.name, pod.status.phase)

        return ""
    assertEventually(readyChecker, 2, 30)
    pod_list = v1.list_namespaced_pod("default", label_selector=lSel)
    pod_names = []
    for pod in pod_list.items:
        pod_names.append(pod.metadata.name)
    return pod_names

def deleteDs(name):
    lSel = "name={}".format(name)
    av1 = client.AppsV1Api()
    body = client.V1DeleteOptions()
    av1.delete_namespaced_daemon_set(name, "default", body=body)
    v1 = client.CoreV1Api()
    def doneChecker():
        pod_list = v1.list_namespaced_pod("default", label_selector=lSel)
        if len(pod_list.items) != 0:
            return "still present"
        return ""

    assertEventually(doneChecker, 2, 45)

def getPodIPs(ns, selector):
    v1 = client.CoreV1Api()
    ips = []
    pod_list = v1.list_namespaced_pod(ns, label_selector=selector)
    for pod in pod_list.items:
        ips.append(pod.status.pod_ip)
    return ips

def read_gbps_tunnel_ids():
    api_c = client.ApiClient()
    custom_c = client.CustomObjectsApi(api_c)
    api_response = custom_c.get_namespaced_custom_object_status("aci.aw", "v1", "kube-system", "gbpsstates", "gbp-server")
    #print(api_response)
    return api_response['status']['tunnel-ids']

def read_vrf_encap_id():
    v1 = client.CoreV1Api()
    resp = v1.read_namespaced_config_map("aci-containers-config", getCniNs())
    gbp_cfg = json.loads(resp.data['gbp-server-config'])
    return gbp_cfg['vrf-encap-id']

def getCniNs():
    if namespaceExists("aci-containers-system"):
        return "aci-containers-system"

    return "kube-system"

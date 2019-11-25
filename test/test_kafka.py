import pytest
import logging
import tutils
from kubernetes import client, config, utils
from kubernetes.stream import stream
from kubernetes.client import configuration
from kubernetes.client.rest import ApiException
from os import path
from time import sleep
import yaml

tutils.logSetup()
configuration.assert_hostname = False
config.load_kube_config()
configuration.assert_hostname = False
k8s_client = client.ApiClient()

def getPodNames(kapi, ns, selector):
    names = []
    pod_list = kapi.list_namespaced_pod(ns, label_selector=selector)
    for pod in pod_list.items:
        if pod.status.phase == "Running":
            names.append(pod.metadata.name)
    return names

# yaml filename must match the object name, per our convention
def nameToYaml(name):
    return "yamls/{}.yaml".format(name)

def assertPodReady(ns, name, timeout):
    v1 = client.CoreV1Api()

    # check pod is ready
    def podChecker():
        s = v1.read_namespaced_pod_status(name, ns)
        if s.status.phase != "Running":
            return "Pod not running"
            
        for cond in s.status.conditions:
            if cond.type == "ContainersReady" and cond.status == "True":
                return ""

        return "Containers not ready"

    tutils.assertEventually(podChecker, 1, timeout)

def kafkaChecker(epKeys, respWord, attempts=2):
    v1 = client.CoreV1Api()
    def checkIt():
        for epid in epKeys:
            cmd = ['./kafkakv', '-key']
            cmd.append(epid)
            resp = stream(v1.connect_get_namespaced_pod_exec, "kafkakv", 'default', command=cmd, stderr=True, stdin=False, stdout=True, tty=False)
            #print("cmd:{} resp: {}".format(cmd, resp))
            if respWord in resp and epid in resp:
                continue

            return "{} -- not yet {}".format(epid, respWord)

        return ""

    tutils.assertEventually(checkIt, 1, attempts)

kafkaYamls = ["zookeeper-ss.yaml", "zookeeper-hl.yaml", "zookeeper-svc.yaml", "kafka-ss.yaml", "kafka-hl.yaml", "kafka-svc.yaml", "kkv.yaml"]

class TestKafkaInterface(object):

    def test_kafka(object):
        # setup kafka services
        tutils.tcLog("Setup kafka services")
        for ky in kafkaYamls:
            try:
                utils.create_from_yaml(k8s_client, "yamls/"+ky)
            except ApiException as e:
                logging.debug("{} - ignored".format(e.reason))

        k8s_api = client.CoreV1Api()
        # check kafka server is ready
        assertPodReady("default", "ut-kafka-0", 120)

        # collect the current list of ep's from k8s
        tutils.tcLog("Collect ep's from k8s")
        crdApi = client.CustomObjectsApi()
        group = "aci.aw"
        ns = "kube-system"
        epList = crdApi.list_namespaced_custom_object(group, "v1", ns, "podifs")
        kafkaEPList = []
        for k, eps in epList.items():
            if type(eps) is not list:
                continue
            for ep in eps:
                epStatus = tutils.SafeDict(ep['status'])
                if epStatus['podns'] is 'missing':
                    logging.debug("MarkerID is {}".format(epStatus['containerID']))
                    continue

                epID = "{}.{}.{}".format(epStatus['podns'],epStatus['podname'], epStatus['ifname'])
                kafkaEPList.append(epID)

        logging.debug("EPList is {}".format(kafkaEPList))

        tutils.tcLog("Verifying initial EPList with kafka")
        kafkaChecker(kafkaEPList, "found")

        tutils.tcLog("Adding a pod and checking it in kafka")
        utils.create_from_yaml(k8s_client, "yamls/alpine-pod.yaml")
        assertPodReady("default", "alpine-pod", 45)
        tutils.tcLog("Check for podif")
        p = crdApi.get_namespaced_custom_object(group, "v1", ns, "podifs", "default.alpine-pod")
        logging.debug("podif: {}".format(p))
        epStatus = p['status']
        epID = "{}.{}.{}".format(epStatus['podns'], epStatus['podname'], epStatus['ifname'])
        toCheck = []
        toCheck.append(epID)
        kafkaChecker(toCheck, "found")
        tutils.tcLog("Deleting a pod and checking removal from kafka")
        k8s_api.delete_namespaced_pod("alpine-pod", "default", client.V1DeleteOptions())
        kafkaChecker(toCheck, "missing", 8)
	
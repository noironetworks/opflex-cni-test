import pytest
import os
import json
from acc_provision import acc_provision, apic_provision
import logging
import tutils
from time import sleep
from kubernetes import client, config

tutils.logSetup()
#config.load_kube_config()
#v1 = client.CoreV1Api()

def setup_config():
    prov_inp_file = os.getenv('PROV_INP_FILE', "./provision_input.yaml")
    uc = acc_provision.config_user(prov_inp_file)
    admin_pw = os.getenv('PSWD', "")
    uc["aci_config"]["apic_login"] = {}
    uc["aci_config"]["apic_login"]["username"] = "admin"
    uc["aci_config"]["apic_login"]["password"] = admin_pw
    uc["aci_config"]["apic_login"]["timeout"] = None
    uc["aci_config"]["capic"] = True
    uc["aci_config"]["apic_proxy"] = None
    uc["provision"] = {
            "prov_apic": True,
            "debug_apic": False,
            "save_to": "",
            "skip-kafka-certs": True,
        }
    acc_provision.deep_merge(uc, acc_provision.config_default())
    return uc

class TestBasic(object):
    def setup(object):
        object.config = setup_config()
        object.apic = acc_provision.get_apic(object.config)


    def verifyClusterInfo(object):
        vmm_name = object.config["aci_config"]["system_id"]
        tn_name = object.config["aci_config"]["tenant"]["name"]
        tutils.tcLog("Verify cluster info objects for {}/{}".format(tn_name,vmm_name))
        moClasses = ["compClusterInfo", "hcloudClusterInfo"]
        for moCl in moClasses:
            qry = '/api/class/{}.json?query-target-filter=and(eq({}.name,"{}"),eq({}.accountName,"{}"))'.format(moCl, moCl, moCl, vmm_name, tn_name)
            resp = object.apic.get(path=qry)
            resJson = json.loads(resp.content)
            assert len(resJson["imdata"]) == 1

    def countCompHv(object):
        vmm_name = object.config["aci_config"]["system_id"]
        tutils.tcLog("Count compHv objects for {}".format(vmm_name))
        qry = '/api/mo/comp/prov-Kubernetes/ctrlr-[{}]-{}.json?query-target=children&target-subtree-class=compHv'.format(vmm_name, vmm_name)
        resp = object.apic.get(path=qry)
        resJson = json.loads(resp.content)
        return len(resJson["imdata"])

    def countCSR(object):
        tutils.tcLog("Count csr objects")
        qry = '/api/class/hcloudCsr.json'
        resp = object.apic.get(path=qry)
        resJson = json.loads(resp.content)
        return len(resJson["imdata"])

    def getCniTep(object):
        tutils.tcLog("Get CniTep objects")
        qry = '/api/class/hcl3CniTep.json'
        resp = object.apic.get(path=qry)
        resJson = json.loads(resp.content)
        print(resJson)

    def test_concrete(object):
        object.setup()
        addr = object.config["aci_config"]["apic_hosts"][0]
        tutils.tcLog("Verify capic {} access".format(addr))
        assert object.apic is not None
        object.verifyClusterInfo()
        nodeCount = object.countCompHv()
        print("nodeCount: {}".format(nodeCount))
        csrCount = object.countCSR()
        print("csrCount: {}".format(csrCount))
        if nodeCount == 0 or csrCount == 0:
            print("No further tests possible with these csr/compHv count")
            return
        object.getCniTep() 
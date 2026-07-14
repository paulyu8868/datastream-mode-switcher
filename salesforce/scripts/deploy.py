"""
sf CLI 없이 Python으로 Metadata API(SOAP) 배포. 2단계 배포.
  Phase 1: CustomObject(CMDT 타입) + Apex 클래스 (RunSpecifiedTests)
  Phase 2: CustomMetadata 레코드  (타입이 실제 존재해야 하므로 별도 배포)
root .env 의 client_credentials(또는 password) 토큰을 sessionId로 사용.

사용:
  python deploy.py            # Phase 1 을 checkOnly 검증만 (변경 없음)
  python deploy.py --deploy   # Phase 1 실제 배포 -> 성공 시 Phase 2 실제 배포
"""
import argparse
import base64
import io
import os
import re
import sys
import time
import zipfile

import requests
from dotenv import load_dotenv

MD_API = "62.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "force-app", "main", "default")


def core_token():
    load_dotenv(os.path.join(HERE, "..", "..", ".env"))
    lu = os.environ["SF_LOGIN_URL"].rstrip("/")
    cid = os.environ["SF_CLIENT_ID"]; cs = os.environ["SF_CLIENT_SECRET"]
    un = os.environ.get("SF_USERNAME"); pw = os.environ.get("SF_PASSWORD_AND_TOKEN")
    data = ({"grant_type": "password", "client_id": cid, "client_secret": cs,
             "username": un, "password": pw} if (un and pw)
            else {"grant_type": "client_credentials", "client_id": cid, "client_secret": cs})
    r = requests.post(f"{lu}/services/oauth2/token", data=data); r.raise_for_status()
    j = r.json()
    return j["access_token"], j["instance_url"]


def read(*parts):
    with open(os.path.join(SRC, *parts), encoding="utf-8") as f:
        return f.read()


OBJECT_MDAPI = """<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Data Stream Profile</label>
    <pluralLabel>Data Stream Profiles</pluralLabel>
    <visibility>Public</visibility>
    <fields>
        <fullName>Stream_Record_Id__c</fullName>
        <fieldManageability>DeveloperControlled</fieldManageability>
        <label>Stream Record Id</label>
        <length>18</length>
        <required>true</required>
        <type>Text</type>
        <unique>false</unique>
    </fields>
    <fields>
        <fullName>Refresh_Mode__c</fullName>
        <fieldManageability>DeveloperControlled</fieldManageability>
        <label>Refresh Mode</label>
        <length>40</length>
        <required>true</required>
        <type>Text</type>
        <unique>false</unique>
    </fields>
    <fields>
        <fullName>Import_Directory__c</fullName>
        <fieldManageability>DeveloperControlled</fieldManageability>
        <label>Import Directory</label>
        <length>255</length>
        <required>false</required>
        <type>Text</type>
        <unique>false</unique>
    </fields>
    <fields>
        <fullName>File_Name__c</fullName>
        <fieldManageability>DeveloperControlled</fieldManageability>
        <label>File Name</label>
        <length>100</length>
        <required>false</required>
        <type>Text</type>
        <unique>false</unique>
    </fields>
</CustomObject>
"""

PKG1 = f"""<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types><members>DataStreamModeFlipper</members><members>DataStreamModeFlipperTest</members><name>ApexClass</name></types>
    <types><members>Data_Stream_Profile__mdt</members><name>CustomObject</name></types>
    <version>{MD_API}</version>
</Package>
"""

PKG2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types><members>Data_Stream_Profile.DAILY</members><members>Data_Stream_Profile.MONTHLY</members><name>CustomMetadata</name></types>
    <version>{MD_API}</version>
</Package>
"""


def zip_b64(package_xml, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", package_xml)
        for path, content in files:
            z.writestr(path, content)
    return base64.b64encode(buf.getvalue()).decode()


def soap(instance_url, token, body_inner):
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header><met:SessionHeader><met:sessionId>{token}</met:sessionId></met:SessionHeader></soapenv:Header>
  <soapenv:Body>{body_inner}</soapenv:Body>
</soapenv:Envelope>"""
    return requests.post(f"{instance_url}/services/Soap/m/{MD_API}",
                         data=envelope.encode("utf-8"),
                         headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})


def run_deploy(instance_url, token, z_b64, check_only, test_level, run_tests=None):
    rt = f"<met:runTests>{run_tests}</met:runTests>" if run_tests else ""
    body = f"""<met:deploy>
      <met:ZipFile>{z_b64}</met:ZipFile>
      <met:DeployOptions>
        <met:rollbackOnError>true</met:rollbackOnError>
        <met:testLevel>{test_level}</met:testLevel>{rt}
        <met:checkOnly>{'true' if check_only else 'false'}</met:checkOnly>
        <met:singlePackage>true</met:singlePackage>
      </met:DeployOptions>
    </met:deploy>"""
    r = soap(instance_url, token, body)
    if not r.ok:
        print("deploy 요청 실패:", r.status_code, r.text[:1000]); return False
    m = re.search(r"<id>(.*?)</id>", r.text)
    if not m:
        print("deploy id 파싱 실패:", r.text[:1000]); return False
    async_id = m.group(1)
    print("  deploy id:", async_id)
    for _ in range(60):
        time.sleep(5)
        s = soap(instance_url, token,
                 f"<met:checkDeployStatus><met:asyncProcessId>{async_id}</met:asyncProcessId>"
                 f"<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
        if not re.search(r"<done>true</done>", s.text):
            continue
        st = re.search(r"<status>(.*?)</status>", s.text)
        st = st.group(1) if st else "?"
        ok = st in ("Succeeded", "SucceededPartial")
        print("  status:", st, "✅" if ok else "❌")
        em = re.search(r"<errorMessage>(.*?)</errorMessage>", s.text, re.S)
        if em:
            print("  errorMessage:", em.group(1))
        for fm in re.finditer(r"<componentFailures>(.*?)</componentFailures>", s.text, re.S):
            b = fm.group(1)
            fn = re.search(r"<fullName>(.*?)</fullName>", b)
            pr = re.search(r"<problem>(.*?)</problem>", b)
            print("   - FAIL", fn and fn.group(1), ":", pr and pr.group(1))
        for fm in re.finditer(r"<failures>(.*?)</failures>", s.text, re.S):
            b = fm.group(1)
            nm = re.search(r"<methodName>(.*?)</methodName>", b)
            msg = re.search(r"<message>(.*?)</message>", b)
            print("   - TEST FAIL", nm and nm.group(1), ":", msg and msg.group(1))
        return ok
    print("  ⚠️ 폴링 타임아웃"); return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", action="store_true", help="실제 배포(미지정 시 Phase1 checkOnly 검증만)")
    args = ap.parse_args()

    token, instance_url = core_token()
    print(f"instance_url = {instance_url}\n")

    files1 = [("objects/Data_Stream_Profile__mdt.object", OBJECT_MDAPI)]
    for f in ("DataStreamModeFlipper.cls", "DataStreamModeFlipper.cls-meta.xml",
              "DataStreamModeFlipperTest.cls", "DataStreamModeFlipperTest.cls-meta.xml"):
        files1.append((f"classes/{f}", read("classes", f)))
    z1 = zip_b64(PKG1, files1)

    if not args.deploy:
        print("[Phase 1] checkOnly 검증 (오브젝트+클래스, RunSpecifiedTests)")
        run_deploy(instance_url, token, z1, True, "RunSpecifiedTests", "DataStreamModeFlipperTest")
        print("\n(레코드 Phase 2는 오브젝트가 실제 커밋돼야 검증 가능 — --deploy 시 자동 진행)")
        return

    print("[Phase 1] 실제 배포 (오브젝트+클래스, RunSpecifiedTests)")
    if not run_deploy(instance_url, token, z1, False, "RunSpecifiedTests", "DataStreamModeFlipperTest"):
        print("Phase 1 실패 → 중단"); sys.exit(1)

    files2 = [
        ("customMetadata/Data_Stream_Profile.DAILY.md", read("customMetadata", "Data_Stream_Profile.DAILY.md-meta.xml")),
        ("customMetadata/Data_Stream_Profile.MONTHLY.md", read("customMetadata", "Data_Stream_Profile.MONTHLY.md-meta.xml")),
    ]
    z2 = zip_b64(PKG2, files2)
    print("\n[Phase 2] 실제 배포 (CustomMetadata 레코드)")
    if not run_deploy(instance_url, token, z2, False, "NoTestRun"):
        print("Phase 2 실패"); sys.exit(1)
    print("\n🎉 전체 배포 완료")


if __name__ == "__main__":
    main()

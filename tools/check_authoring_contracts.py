"""对制作契约执行字节、资源隔离与像素参考检查。"""
from __future__ import annotations
import base64
import copy
import hashlib
import json
from pathlib import Path
from PIL import Image
from authoring_contract_checks import authoring_errors, validate
from canonical_json import loads
from check_docs import workflow_errors

ROOT=Path(__file__).resolve().parents[1]
FRAME=1048576
CHUNK=196608
LIMIT=33554432

def raw(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')

def sha(value):
    return hashlib.sha256(value).hexdigest()

class ContentReferenceModel:
    """制作契约检查使用的内存状态。"""
    def __init__(self,data,purpose='workflowModel',owner='studio',workspace='sample'):
        if not 0<len(data)<=LIMIT:raise ValueError('length')
        self.length,self.sha,self.purpose=len(data),sha(data),purpose
        self.owner,self.workspace=owner,workspace
        self.parts,self.received,self.expired=[],{},False
    def write(self,offset,data,digest=None):
        if self.expired:raise ValueError('expired')
        if not 0<len(data)<=CHUNK or (digest is not None and digest!=sha(data)):raise ValueError('chunk')
        if offset in self.received:
            if self.received[offset]!=data:raise ValueError('changed replay')
            return True
        if offset!=sum(map(len,self.parts)) or offset+len(data)>self.length:raise ValueError('offset')
        self.parts.append(data);self.received[offset]=data
        return False
    def seal(self,owner='studio',workspace='sample',purpose='workflowModel'):
        if self.expired or (owner,workspace,purpose)!=(self.owner,self.workspace,self.purpose):raise ValueError('binding')
        data=b''.join(self.parts)
        if len(data)!=self.length or sha(data)!=self.sha:raise ValueError('identity')
        value=loads(data.decode('utf-8'))
        ref='urn:deskweave:content:1#/$defs/workflowModel' if purpose=='workflowModel' else 'urn:deskweave:trajectory:1'
        if validate(value,ref):raise ValueError('purpose')
        return {'kind':'resource','resourceId':'reference-large-model','length':len(data),'sha256':sha(data)}

def main():
    checks=[]
    def check(name,result):
        assert result,name
        checks.append(name)
    def reject(name,fn):
        try:fn()
        except (ValueError,UnicodeError):checks.append(name);return
        raise AssertionError(name)
    definition=json.loads((ROOT/'examples/workflows/hello.json').read_text(encoding='utf-8'))
    definition['variables']['largeSample']=['样例'*100]*2048
    model={'definition':definition,'dependencies':{}}
    payload=raw(model)
    check('合法大流程结构',not validate(model,'urn:deskweave:content:1#/$defs/workflowModel'))
    check('合法大流程语义',not workflow_errors(definition))
    params={'workflowId':definition['id'],'draftVersion':1,'baseRevision':None,**model}
    check('内嵌保存参数结构',not validate(params,'urn:deskweave:service:1#/$defs/workflow.save/properties/params'))
    request=json.loads((ROOT/'examples/messages/request.json').read_text(encoding='utf-8'))
    request.update(method='workflow.save',params=params)
    check('完整内嵌信封超出1MiB',len(raw(request))>FRAME)
    def wire(value):
        if len(raw(value))>FRAME:raise ValueError('REQUEST_TOO_LARGE')
        return raw(value)
    reject('内嵌发送明确拒绝',lambda:wire(request))
    reference=ContentReferenceModel(payload)
    chunks=[payload[i:i+CHUNK] for i in range(0,len(payload),CHUNK)]
    offset=0
    for chunk in chunks:
        envelope=copy.deepcopy(request);envelope.update(method='content.write',params={'uploadId':'large','offset':offset,'chunk':base64.b64encode(chunk).decode('ascii'),'chunkSha256':sha(chunk)})
        check('分块信封预算-'+str(offset),len(wire(envelope))<FRAME)
        reference.write(offset,chunk,sha(chunk));offset+=len(chunk)
    check('相同确认块重放',reference.write(0,chunks[0]) is True)
    reject('重放改变内容拒绝',lambda:reference.write(0,b'X'+chunks[0][1:]))
    ref=reference.seal()
    check('分块重组身份',ref['length']==len(payload) and ref['sha256']==sha(payload))
    params={k:v for k,v in params.items() if k not in {'definition','dependencies'}};params['modelRef']=ref
    request['params']=params
    check('资源保存结构',not validate(params,'urn:deskweave:service:1#/$defs/workflow.save/properties/params'))
    check('引用信封预算',len(wire(request))<FRAME)
    missing=ContentReferenceModel(payload);missing.write(0,chunks[0]);reject('缺块不能封存',lambda:missing.seal())
    unordered=ContentReferenceModel(payload);reject('乱序块拒绝',lambda:unordered.write(CHUNK,chunks[1]))
    reject('块摘要篡改拒绝',lambda:unordered.write(0,chunks[0],'0'*64))
    reject('解码块超限拒绝',lambda:unordered.write(0,b'x'*(CHUNK+1)))
    changed=ContentReferenceModel(payload)
    for i,c in enumerate(chunks):changed.write(i*CHUNK,(b'X'+c[1:]) if i==0 else c)
    reject('整段篡改不能封存',lambda:changed.seal())
    reject('跨所有者拒绝',lambda:reference.seal(owner='other'))
    reject('跨工作空间拒绝',lambda:reference.seal(workspace='other'))
    reject('跨用途拒绝',lambda:reference.seal(purpose='trajectoryEdit'))
    reference.expired=True;reject('过期不能封存',lambda:reference.seal())
    reject('32MiB内容边界',lambda:ContentReferenceModel(b'x'*(LIMIT+1)))
    invalid=ContentReferenceModel(b'{"events":[]}');invalid.write(0,b'{"events":[]}');reject('用途结构重新检查',lambda:invalid.seal())
    duplicate=b'{"definition":{},"definition":{},"dependencies":{}}';invalid=ContentReferenceModel(duplicate);invalid.write(0,duplicate);reject('封存拒绝重复键',lambda:invalid.seal())
    base=ROOT/'examples'
    page=json.loads((base/'authoring/variables-page.json').read_text(encoding='utf-8'))
    context={'query':{'runId':page['runId'],'frameId':page['frameId'],'scope':page['scope'],'snapshotId':page['snapshotId'],'cursor':None,'pageSize':100},'dataVersion':8}
    check('变量固定快照',not authoring_errors(page,'variablesPage',context))
    for key,value in [('runId','other'),('frameId','child'),('scope','outputs'),('snapshotId','new')]:
        wrong=copy.deepcopy(context);wrong['query'][key]=value
        check('拒绝跨绑定变量页-'+key,bool(authoring_errors(page,'variablesPage',wrong)))
    wrong=copy.deepcopy(context);wrong['now']=page['expiresAt'];check('快照到期拒绝',bool(authoring_errors(page,'variablesPage',wrong)))
    wrong=copy.deepcopy(context);wrong['dataVersion']=9;check('数据版本变化不混页',bool(authoring_errors(page,'variablesPage',wrong)))
    plan=json.loads((base/'authoring/asset-image-plan.json').read_text(encoding='utf-8'))
    commit=json.loads((base/'authoring/asset-image-commit.json').read_text(encoding='utf-8'))
    source=(base/'authoring/source.png').read_bytes();before=sha(source)
    with Image.open(base/'authoring/source.png') as image,Image.open(base/'authoring/cropped.png') as output:
        r=commit['content']['roi'];crop=image.crop((r['x'],r['y'],r['x']+r['width'],r['y']+r['height']))
        check('裁剪像素完全一致',crop.mode==output.mode and crop.size==output.size and crop.tobytes()==output.tobytes())
    check('原图字节保持',sha((base/'authoring/source.png').read_bytes())==before==plan['sourceSha256'])
    print(f'制作契约：{len(checks)} 项通过；模型 {len(payload)} 字节，内嵌信封 {len(raw({**request,"params":{"workflowId":definition["id"],"draftVersion":1,"baseRevision":None,**model}}))} 字节，{len(chunks)} 个块')
    return 0

if __name__=='__main__':raise SystemExit(main())

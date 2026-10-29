"use strict";
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {createHash,createPublicKey,verify}=require('node:crypto');
const base=path.resolve(__dirname,'../examples/updates');
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
function strictJSON(bytes){
 if(bytes.length>1048576||bytes.subarray(0,3).equals(Buffer.from([239,187,191])))throw Error('invalid JSON bytes');
 const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);let at=0;
 const ws=()=>{while(/[\x20\t\r\n]/.test(text[at]||'!'))at++;};
 function string(){
  const start=at++;let escaped=false;
  while(at<text.length){const c=text[at++];if(c==='"'&&!escaped){const result=JSON.parse(text.slice(start,at));for(const scalar of result){const code=scalar.codePointAt(0);if(code>=0xd800&&code<=0xdfff)throw Error('isolated surrogate');}return result;}if(c==='\\'&&!escaped)escaped=true;else escaped=false;}
  throw Error('unterminated string');
 }
 function value(depth=0){
  if(depth>64)throw Error('JSON nesting limit');ws();const c=text[at];
  if(c==='"')return string();
  if(c==='{'){
   at++;ws();const result=Object.create(null),seen=new Set();if(text[at]==='}'){at++;return result;}
   for(;;){ws();if(text[at]!=='"')throw Error('object key');const key=string();if(seen.has(key))throw Error('duplicate key');seen.add(key);ws();if(text[at++]!==':')throw Error('object separator');result[key]=value(depth+1);ws();const tail=text[at++];if(tail==='}')return result;if(tail!==',')throw Error('object ending');}
  }
  if(c==='['){at++;ws();const result=[];if(text[at]===']'){at++;return result;}for(;;){result.push(value(depth+1));ws();const tail=text[at++];if(tail===']')return result;if(tail!==',')throw Error('array ending');}}
  for(const [token,result] of [['true',true],['false',false],['null',null]])if(text.startsWith(token,at)){at+=token.length;return result;}
  const match=/^(0|[1-9][0-9]*)(?=[\x20\t\r\n,}\]]|$)/.exec(text.slice(at));
  if(!match)throw Error('integer encoding');at+=match[0].length;const result=Number(match[0]);if(!Number.isSafeInteger(result))throw Error('integer range');return result;
 }
 const result=value();ws();if(at!==text.length)throw Error('trailing JSON');return result;
}
function canonical(value){
 if(value===null||typeof value==='boolean'||typeof value==='string')return JSON.stringify(value);
 if(typeof value==='number'){assert(Number.isSafeInteger(value)&&value>=0&&!Object.is(value,-0));return String(value);}
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 assert(value&&typeof value==='object');const keys=Object.keys(value).sort();assert(keys.every(key=>/^[\x20-\x7e]+$/.test(key)));
 return '{'+keys.map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
}
const canonicalBytes=value=>Buffer.from(canonical(value),'utf8');
const signedBytes=envelope=>Buffer.concat([Buffer.from('DeskWeaveStudio/update/v1\n'+envelope.signed.role+'\n','utf8'),canonicalBytes(envelope.signed)]);
function read(name){const target=path.resolve(base,name);assert(target.startsWith(base+path.sep));return strictJSON(fs.readFileSync(target));}
function decode(value,length){const bytes=Buffer.from(value,'base64');assert.equal(bytes.toString('base64'),value);assert.equal(bytes.length,length);return bytes;}
function keysFor(root){
 const map=new Map();for(const key of root.keys){assert.equal(key.algorithm,'Ed25519');const raw=decode(key.publicKey,32);assert.equal(hash(raw),key.keyId);assert(!map.has(key.keyId));map.set(key.keyId,createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),raw]),format:'der',type:'spki'}));}
 const used=new Set();for(const role of ['root','stable','preview']){const grant=root.roles[role];assert(Number.isInteger(grant.threshold)&&grant.threshold>=1&&grant.threshold<=grant.keyIds.length);assert.equal(new Set(grant.keyIds).size,grant.keyIds.length);for(const id of grant.keyIds){assert(map.has(id)&&!used.has(id));used.add(id);}}
 assert.equal(root.roles.root.threshold,2);assert.equal(root.roles.root.keyIds.length,3);return map;
}
function fail(code){throw Object.assign(Error(code),{code});}
function threshold(envelope,root,role,revoked=new Set(),failure='signatureInvalid'){
 const keys=keysFor(root),grant=root.roles[role],valid=new Set(),unusable=new Set();
 for(const signature of envelope.signatures){if(!grant.keyIds.includes(signature.keyId))continue;let good=false;try{good=verify(null,signedBytes(envelope),keys.get(signature.keyId),decode(signature.signature,64));}catch{}if(good)(revoked.has(signature.keyId)?unusable:valid).add(signature.keyId);}
 if(valid.size<grant.threshold)fail(valid.size+unusable.size>=grant.threshold?'revoked':failure);return valid.size;
}
const time=value=>{const result=Date.parse(value);assert(Number.isFinite(result));return result;};
function lifetime(signed,now){
 const start=time(signed.issuedAt),end=time(signed.expiresAt),days={root:180,release:7,revocations:1}[signed.role];
 if(start>now+300000)fail('clockInvalid');if(end<=start||end-start>days*86400000||now>=end)fail('metadataExpired');
}
function evaluate(vector,cache){
 try{
  const state=vector.trustState,asOf=time(vector.asOf),last=time(state.lastTrustedTime);if(asOf<last-300000)fail('clockInvalid');const now=Math.max(asOf,last);
  const embedded=read(vector.embeddedRoot),history=state.rootHistory,historyFiles=vector.rootHistoryFiles;
  if(!Array.isArray(history)||!history.length||!Array.isArray(historyFiles)||historyFiles.length!==history.length)fail('trustUnavailable');
  const online=new Set(),rootKeys=new Set();let rootEnvelope,root;
  for(let index=0;index<history.length;index++){
   const envelope=read(historyFiles[index]),body=envelope.signed,record=history[index];
   if(body.role!=='root'||body.version!==record.version||hash(canonicalBytes(body))!==record.sha256)fail('trustUnavailable');
   if(index===0){
    if(hash(canonicalBytes(body))!==hash(canonicalBytes(embedded.signed)))fail('trustUnavailable');
    threshold(envelope,body,'root',new Set(),'trustUnavailable');
   }else{
    if(body.version!==root.version+1)fail('trustUnavailable');
    threshold(envelope,root,'root',new Set(),'trustUnavailable');threshold(envelope,body,'root',new Set(),'trustUnavailable');
    for(const channel of ['stable','preview'])if(body.minimumSecuritySequence[channel]<root.minimumSecuritySequence[channel])fail('replayed');
   }
   rootEnvelope=envelope;root=body;for(const id of root.roles.root.keyIds)rootKeys.add(id);for(const channel of ['stable','preview'])for(const id of root.roles[channel].keyIds)online.add(id);
  }
  const expectedCurrent=read(vector.trustedRoot).signed;
  if(root.version!==state.rootVersion||hash(canonicalBytes(root))!==state.rootSha256||hash(canonicalBytes(expectedCurrent))!==state.rootSha256)fail('trustUnavailable');
  lifetime(root,now);
  for(const filename of vector.rootUpdates){
   const next=read(filename),body=next.signed;if(body.role!=='root'||body.version!==root.version+1)fail('trustUnavailable');
   threshold(next,root,'root',new Set(),'trustUnavailable');threshold(next,body,'root',new Set(),'trustUnavailable');lifetime(body,now);
   for(const channel of ['stable','preview'])if(body.minimumSecuritySequence[channel]<root.minimumSecuritySequence[channel])fail('replayed');
   rootEnvelope=next;root=body;for(const id of root.roles.root.keyIds)rootKeys.add(id);for(const channel of ['stable','preview'])for(const id of root.roles[channel].keyIds)online.add(id);
  }
  if(!vector.revocations)fail('trustUnavailable');const revokeEnvelope=read(vector.revocations),revoke=revokeEnvelope.signed;
  if(revoke.role!=='revocations'||revoke.rootVersion!==root.version)fail('trustUnavailable');threshold(revokeEnvelope,root,'root');lifetime(revoke,now);
  const revokeHash=hash(canonicalBytes(revoke));if(revoke.version<state.revocationsVersion||revoke.version===state.revocationsVersion&&revokeHash!==state.revocationsSha256)fail('replayed');
  const previous=cache.get(state.revocationsSha256);if(!previous)fail('trustUnavailable');
  for(const id of revoke.revokedKeyIds)if(rootKeys.has(id)||!online.has(id))fail('trustUnavailable');
  if(previous.revokedKeyIds.some(id=>!revoke.revokedKeyIds.includes(id))||previous.revokedArtifacts.some(id=>!revoke.revokedArtifacts.includes(id)))fail('replayed');
  for(const channel of ['stable','preview'])if(revoke.minimumSecuritySequence[channel]<previous.minimumSecuritySequence[channel])fail('replayed');
  const envelope=read(vector.release),release=envelope.signed;if(release.role!=='release'||release.rootVersion!==root.version)fail('trustUnavailable');
  if(release.channel!==vector.selectedChannel)fail('channelMismatch');threshold(envelope,root,release.channel,new Set(revoke.revokedKeyIds));lifetime(release,now);
  if(revoke.revokedArtifacts.includes(release.artifact.sha256))fail('revoked');
  const floor=Math.max(root.minimumSecuritySequence[release.channel],revoke.minimumSecuritySequence[release.channel]);
  if(release.sequence<floor||release.sequence<state.channelSequences[release.channel])fail('replayed');
  if(release.sequence===state.channelSequences[release.channel]&&hash(canonicalBytes(release))!==state.channelManifestHashes[release.channel])fail('replayed');
  const artifact=fs.readFileSync(path.join(base,'artifact-payload.txt'));assert.equal(artifact.length,release.artifact.length);assert.equal(hash(artifact),release.artifact.sha256);return 'pass';
 }catch(error){if(error.code)return error.code;throw error;}
}
const vectors=read('signature-vectors.json'),cache=new Map();let signatureCount=0;
assert.equal(vectors.canonicalization,'deskweave-update-json-v1');
for(const key of vectors.publicKeys){const raw=decode(key.rawBase64,32);assert.equal(hash(raw),key.keyId);assert.equal(Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),raw]).toString('base64'),key.spkiBase64);}
for(const encoding of vectors.encodingVectors){
 const envelope=read(encoding.file),canonical=canonicalBytes(envelope.signed),message=signedBytes(envelope);assert.equal(canonical.toString('base64'),encoding.canonicalBase64);assert.equal(message.toString('base64'),encoding.signedBytesBase64);assert.equal(hash(canonical),encoding.signedSha256);assert.equal(hash(message),encoding.messageSha256);cache.set(hash(canonical),envelope.signed);
 for(const row of envelope.signatures){const publicKey=vectors.publicKeys.find(key=>key.keyId===row.keyId);assert(publicKey);assert(verify(null,message,createPublicKey({key:Buffer.from(publicKey.spkiBase64,'base64'),format:'der',type:'spki'}),decode(row.signature,64)));signatureCount++;}
}
for(const vector of vectors.vectors)assert.equal(evaluate(vector,cache),vector.expected,vector.name);
const rejected=['{"a":1,"a":2}','{"a":-0}','{"a":-1}','{"a":1.0}','{"a":1e0}','{"a":9007199254740992}','{"a":"\\ud800"}','{"a":1}x','{"a":01}','[true,]'];
for(const raw of rejected)assert.throws(()=>strictJSON(Buffer.from(raw)));
assert.throws(()=>strictJSON(Buffer.from([0xc0,0xaf])));assert.throws(()=>strictJSON(Buffer.from([239,187,191,123,125])));
assert.equal(canonical(strictJSON(Buffer.from('{"b":[0,true,null],"a":"中文\\n\\u0001"}'))),'{"a":"中文\\n\\u0001","b":[0,true,null]}');
console.log(`更新签名：${signatureCount} 份签名、${vectors.encodingVectors.length} 组规范字节、${vectors.vectors.length} 组信任状态向量、${rejected.length+2} 组非法编码检查通过`);

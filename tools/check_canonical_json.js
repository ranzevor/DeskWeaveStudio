"use strict";
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
function scalar(text){for(const c of text){const n=c.codePointAt(0);if(n>=0xd800&&n<=0xdfff)throw Error('invalid Unicode');}return text;}
function canonical(value){
 if(value===null||typeof value==='boolean')return JSON.stringify(value);
 if(typeof value==='number'){if(!Number.isFinite(value))throw Error('non-finite number');return JSON.stringify(value);}
 if(typeof value==='string')return JSON.stringify(scalar(value));
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(scalar(k))+':'+canonical(value[k])).join(',')+'}';
 throw Error('unsupported value');
}
function strictJSON(text){
 let at=0;const whitespace=()=>{while(/[ \t\r\n]/.test(text[at]||'!'))at++;};
 function string(){const start=at++;let escaped=false;while(at<text.length){const c=text[at++];if(!escaped&&c==='"')return scalar(JSON.parse(text.slice(start,at)));if(!escaped&&c==='\\')escaped=true;else escaped=false;}throw Error('unterminated string');}
 function value(){whitespace();const c=text[at];if(c==='"')return string();if(c==='{'){at++;const result=Object.create(null);whitespace();if(text[at]==='}'){at++;return result;}while(true){whitespace();if(text[at]!=='"')throw Error('key expected');const k=string();if(Object.hasOwn(result,k))throw Error('duplicate key');whitespace();if(text[at++]!==':')throw Error('colon expected');result[k]=value();whitespace();const end=text[at++];if(end==='}')return result;if(end!==',')throw Error('separator expected');}}
 if(c==='['){at++;const result=[];whitespace();if(text[at]===']'){at++;return result;}while(true){result.push(value());whitespace();const end=text[at++];if(end===']')return result;if(end!==',')throw Error('separator expected');}}
 for(const [word,result]of [['true',true],['false',false],['null',null]])if(text.startsWith(word,at)){at+=word.length;return result;}
 const number=text.slice(at).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/);if(!number)throw Error('value expected');at+=number[0].length;const result=Number(number[0]);if(!Number.isFinite(result)||!/[.eE]/.test(number[0])&&!Number.isSafeInteger(result))throw Error('invalid number');return result;
 }
 const result=value();whitespace();if(at!==text.length)throw Error('trailing data');return result;
}
const rows=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../examples/canonical/vectors.json'),'utf8')).vectors;let accepted=0,rejected=0;
for(const row of rows){let actual;try{const value=row.float64Hex?Buffer.from(row.float64Hex,'hex').readDoubleBE(0):strictJSON(row.inputJson);actual=canonical(value);}catch(error){assert(row.reject,row.name);rejected++;continue;}assert(!row.reject,row.name);assert.equal(actual,row.canonical,row.name);assert.equal(createHash('sha256').update(actual).digest('hex'),row.sha256,row.name);accepted++;}
console.log(`JavaScript JCS：${accepted} 组规范字节、${rejected} 组拒绝向量通过`);

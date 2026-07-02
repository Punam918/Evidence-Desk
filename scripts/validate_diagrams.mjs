// Syntax validation only. Install mermaid and jsdom in a temporary npm prefix.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const modules=path.resolve(process.argv[2] || 'node_modules');
const require=createRequire(path.join(path.dirname(modules),'package.json'));
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><html><body></body></html>');
globalThis.window=dom.window;
globalThis.document=dom.window.document;
globalThis.navigator=dom.window.navigator;
const {default:mermaid}=await import(pathToFileURL(require.resolve('mermaid')).href);
mermaid.initialize({startOnLoad:false,securityLevel:'strict'});
function files(dir) {
 return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.name.startsWith('.')?[]:e.isDirectory()?files(path.join(dir,e.name)):[path.join(dir,e.name)]);
}
const results=[];
for(const file of files(root).filter(x=>x.endsWith('.md'))) {
 let index=0;
 for(const match of fs.readFileSync(file,'utf8').matchAll(/```mermaid\n([\s\S]*?)```/g)) {
  try {await mermaid.parse(match[1]);results.push({file:path.relative(root,file),index:index++,status:'pass'});}
  catch(error) {results.push({file:path.relative(root,file),index:index++,status:'fail',detail:String(error)});}
 }
}
const report={status:results.every(x=>x.status==='pass')?'pass':'fail',scope:'Mermaid parser syntax only; no visual rendering/layout assessment',mermaid_version:JSON.parse(fs.readFileSync(path.join(modules,'mermaid/package.json'),'utf8')).version,diagrams:results};
fs.writeFileSync(path.join(root,'docs/10-testing/diagram-validation-results.json'),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
process.exitCode=report.status==='pass'?0:1;

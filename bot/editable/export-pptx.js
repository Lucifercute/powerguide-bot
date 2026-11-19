/* Local HTML -> editable OOXML experiment. No rasterized slide backgrounds. */
(() => {
'use strict';
const NS='http://schemas.openxmlformats.org/drawingml/2006/main';
const W=globalThis.POWERGUIDE_EXPORT_CONFIG?.width||1600, H=globalThis.POWERGUIDE_EXPORT_CONFIG?.height||900;
const EMU=7620; // 1600 CSS px -> 13 1/3 inches.
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
const emu=n=>Math.round(n*EMU);
const decode=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
const utf8=s=>new TextEncoder().encode(s);
function normalizeFonts(){
 const {fontCoverage:coverage,fontPolicy:policy}=globalThis.POWERGUIDE_EXPORT_CONFIG;
 if(!coverage)return;
 const walker=document.createTreeWalker(document.getElementById('presentation-viewport'),NodeFilter.SHOW_TEXT),nodes=[];
 while(walker.nextNode())nodes.push(walker.currentNode);
 for(const node of nodes){
  const el=node.parentElement;if(!node.textContent.trim()||el.closest('style,script,[data-pg-font]'))continue;
  const c=getComputedStyle(el),candidates=[...c.fontFamily.split(',').map(x=>x.trim().replace(/["']/g,'')),policy.body,'Inter'];
  const supports=f=>coverage[f]&&[...node.textContent].every(ch=>/\s/.test(ch)||coverage[f].some(([a,b])=>ch.codePointAt(0)>=a&&ch.codePointAt(0)<=b));
  const font=candidates.find(supports)||'Inter';
  const span=el instanceof SVGElement?document.createElementNS(el.namespaceURI,'tspan'):document.createElement('span');
  span.dataset.pgFont=font;span.style.setProperty('font-family',`"${font}"`,'important');
  span.style.setProperty('font-weight',parseInt(c.fontWeight)>=600?'700':'400','important');
  node.replaceWith(span);span.append(node);
 }
}
function opacityOf(el){let alpha=1;for(let p=el;p;p=p.parentElement)alpha*=Number(getComputedStyle(p).opacity);return alpha;}
function applyOpacity(objects,alpha){
 for(const o of objects){
  if(o.type==='image')o.alpha=(o.alpha??1)*alpha;
  for(const c of [o.color,o.stroke?.color,...(o.fill?.stops?o.fill.stops.map(s=>s.color):[o.fill])])if(c)c.alpha=(c.alpha??1)*alpha;
 }
 return objects;
}
function coverCrop(iw,ih,w,h){
 const ir=iw/ih,br=w/h,crop={l:0,t:0,r:0,b:0};
 if(ir>br)crop.l=crop.r=(1-br/ir)/2;else crop.t=crop.b=(1-ir/br)/2;
 return crop;
}
function splitOutside(text,delimiter=',') {
 let depth=0,start=0,parts=[];
 for(let i=0;i<text.length;i++){if(text[i]==='(')depth++;if(text[i]===')')depth--;if(text[i]===delimiter&&!depth){parts.push(text.slice(start,i).trim());start=i+1;}}
 parts.push(text.slice(start).trim());return parts;
}
function color(value){
 if(!value||value==='none'||value==='transparent')return null;
 let m=value.match(/^#([\da-f]{3,8})$/i);
 if(m){let h=m[1];if(h.length===3)h=h.split('').map(c=>c+c).join('');return {hex:h.slice(0,6).toUpperCase(),alpha:h.length===8?parseInt(h.slice(6),16)/255:1};}
 m=value.match(/^rgba?\(([^)]+)\)/i);
 if(m){let a=m[1].split(/[\s,/]+/).filter(Boolean).map(Number);return {hex:a.slice(0,3).map(n=>Math.round(n).toString(16).padStart(2,'0')).join('').toUpperCase(),alpha:a[3]??1};}
 return {hex:'000000',alpha:1};
}
function solid(c){return c?`<a:solidFill><a:srgbClr val="${c.hex}">${c.alpha<1?`<a:alpha val="${Math.round(c.alpha*100000)}"/>`:''}</a:srgbClr></a:solidFill>`:'<a:noFill/>';}
function fillXML(fill){
 if(!fill)return '<a:noFill/>';
 if(!fill.stops)return solid(fill);
 return `<a:gradFill rotWithShape="1"><a:gsLst>${fill.stops.map((s,i)=>`<a:gs pos="${Math.round(s.position*100000)}">${solid(s.color).replace(/^<a:solidFill>|<\/a:solidFill>$/g,'')}</a:gs>`).join('')}</a:gsLst><a:lin ang="${Math.round((((fill.angle??135)-90+360)%360)*60000)}" scaled="1"/></a:gradFill>`;
}
function gradient(value){
 if(!value||!value.startsWith('linear-gradient('))return null;
 const items=splitOutside(value.slice(16,-1));let angle=180;
 if(items[0].match(/deg|^to /)){const first=items.shift();angle=parseFloat(first)||180;}
 const stops=items.map((item,i)=>{
  const match=item.match(/^(rgba?\([^)]+\)|#[\da-f]+)(?:\s+([\d.]+)%)?/i);
  return match?{color:color(match[1]),position:match[2]?Number(match[2])/100:i/(items.length-1)}:null;
 }).filter(Boolean);
 return stops.length>1?{stops,angle}:null;
}
function measureValue(expr,total){
 let terms=expr.replace(/calc\(|\)/g,'').match(/[+-]?\s*\d+(?:\.\d+)?(?:%|px)?/g)||[];
 return terms.reduce((sum,v)=>sum+parseFloat(v.replace(/\s/g,''))*(v.includes('%')?total/100:1),0);
}
function clipPoints(value,w,h){
 if(!value||value==='none')return null;
 if(value.startsWith('polygon('))return splitOutside(value.slice(8,-1)).map(pair=>{
  const bits=pair.match(/calc\([^)]*\)|[^\s]+/g);return [measureValue(bits[0],w),measureValue(bits[1],h)];
 });
 const match=value.match(/^(ellipse|circle)\(([^)]+)\)$/);
 if(!match)return null;
 const halves=match[2].split(/\s+at\s+/),radii=halves[0].trim().split(/\s+/),center=(halves[1]||'50% 50%').trim().split(/\s+/);
 if([...radii,...center].some(v=>!/^[-\d.]+(?:%|px)?$/.test(v)))return null;
 const cx=measureValue(center[0],w),cy=measureValue(center[1]||center[0],h);
 const rx=measureValue(radii[0],w),ry=match[1]==='circle'?rx:measureValue(radii[1]||radii[0],h);
 let points=Array.from({length:96},(_,i)=>[cx+rx*Math.cos(i*Math.PI/48),cy+ry*Math.sin(i*Math.PI/48)]);
 // CSS clips both by the ellipse and the original rectangular paint area.
 for(const [axis,edge,sign] of [[0,0,1],[0,w,-1],[1,0,1],[1,h,-1]]){
  const clipped=[];if(!points.length)return null;
  for(let i=0;i<points.length;i++){
   const a=points[i],b=points[(i+1)%points.length],insideA=(a[axis]-edge)*sign>=-1e-8,insideB=(b[axis]-edge)*sign>=-1e-8;
   if(insideA)clipped.push(a);
   if(insideA!==insideB){const t=(edge-a[axis])/(b[axis]-a[axis]);clipped.push([a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])]);}
  }
  points=clipped;
 }
 return points.length>2?points:null;
}
function strokeXML(stroke){
 if(!stroke||!stroke.color)return '<a:ln><a:noFill/></a:ln>';
 return `<a:ln w="${emu(stroke.width||1)}" cap="${stroke.cap||'rnd'}">${solid(stroke.color)}${stroke.dash?'<a:prstDash val="dash"/>':''}<a:round/></a:ln>`;
}
function transform(o){return `<a:xfrm><a:off x="${emu(o.x)}" y="${emu(o.y)}"/><a:ext cx="${Math.max(1,emu(o.w))}" cy="${Math.max(1,emu(o.h))}"/></a:xfrm>`;}
// CSS clips descendants at the ancestor's padding edge. Preserve that contour
// in native OOXML geometry, including captions, without rasterizing the slide.
function roundedPoints(w,h,radii){
 const r=radii.map(([x,y])=>[Math.max(0,x),Math.max(0,y)]);
 const scale=Math.min(1,w/(r[0][0]+r[1][0]||1),w/(r[3][0]+r[2][0]||1),h/(r[0][1]+r[3][1]||1),h/(r[1][1]+r[2][1]||1));
 r.forEach(a=>{a[0]*=scale;a[1]*=scale;});
 const centers=[[r[0][0],r[0][1]],[w-r[1][0],r[1][1]],[w-r[2][0],h-r[2][1]],[r[3][0],h-r[3][1]]];
 const points=[];
 for(let k=0;k<4;k++){
  const [rx,ry]=r[k],[cx,cy]=centers[k];
  if(!rx||!ry){points.push([[0,0],[w,0],[w,h],[0,h]][k]);continue;}
  const steps=Math.max(6,Math.min(64,Math.ceil(Math.sqrt(Math.max(rx,ry)/.08))));
  for(let i=0;i<=steps;i++){const a=Math.PI+k*Math.PI/2+i*Math.PI/2/steps;points.push([cx+rx*Math.cos(a),cy+ry*Math.sin(a)]);}
 }
 return points;
}
function cssRadii(c,w,h){
 return ['TopLeft','TopRight','BottomRight','BottomLeft'].map(k=>{
  const v=c[`border${k}Radius`].split(/\s+/);return [measureValue(v[0],w),measureValue(v[1]||v[0],h)];
 });
}
function cssGeometry(c,box){
 const r=cssRadii(c,box.w,box.h);
 if(r.every(([x,y])=>!x||!y))return {geometry:'rect'};
 if(r.every(([x,y])=>x===r[0][0]&&y===r[0][1]&&x===y))return {geometry:'roundRect',radius:Math.min(r[0][0],box.w/2,box.h/2)};
 if(r.every(([x,y])=>x>=box.w/2&&y>=box.h/2))return {geometry:'ellipse'};
 return {paths:[{points:roundedPoints(box.w,box.h,r),close:true}]};
}
const cross=(a,b,p)=>(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
function intersectPolygon(points,clip){
 // Clipping contours are convex rounded boxes/ellipses (clockwise in CSS).
 if(clip.reduce((sum,a,i)=>{const b=clip[(i+1)%clip.length];return sum+a[0]*b[1]-b[0]*a[1];},0)<0)clip=[...clip].reverse();
 let result=points;
 for(let i=0;i<clip.length&&result.length;i++){
  const a=clip[i],b=clip[(i+1)%clip.length],next=[];
  if(Math.hypot(b[0]-a[0],b[1]-a[1])<1e-7)continue;
  for(let j=0;j<result.length;j++){
   const p=result[j],q=result[(j+1)%result.length],dp=cross(a,b,p),dq=cross(a,b,q);
   if(dp>=-1e-7)next.push(p);
   if((dp>=-1e-7)!==(dq>=-1e-7)){const t=dp/(dp-dq);next.push([p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])]);}
  }
  result=next;
 }
 return result;
}
function intersectLine(p,q,clip){
 if(clip.reduce((sum,a,i)=>{const b=clip[(i+1)%clip.length];return sum+a[0]*b[1]-b[0]*a[1];},0)<0)clip=[...clip].reverse();
 let lo=0,hi=1;
 for(let i=0;i<clip.length;i++){
  const a=clip[i],b=clip[(i+1)%clip.length],d=cross(a,b,p),delta=cross(a,b,q)-d;
  if(Math.abs(delta)<1e-9){if(d< -1e-7)return null;continue;}
  if(delta>0)lo=Math.max(lo,-d/delta);else hi=Math.min(hi,-d/delta);
  if(lo>hi)return null;
 }
 return [lo,hi].map(t=>[p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])]);
}
function overflowContour(el,root){
 const c=getComputedStyle(el);
 if(!['hidden','clip','scroll','auto'].includes(c.overflowX)||!['hidden','clip','scroll','auto'].includes(c.overflowY))return null;
 const b=rect(el,root),inset=['Left','Top','Right','Bottom'].map(s=>parseFloat(c[`border${s}Width`])||0);
 const [l,t,r,d]=inset,w=Math.max(0,b.w-l-r),h=Math.max(0,b.h-t-d);
 const radii=cssRadii(c,b.w,b.h).map(([x,y],i)=>[Math.max(0,x-(i===0||i===3?l:r)),Math.max(0,y-(i<2?t:d))]);
 return roundedPoints(w,h,radii).map(([x,y])=>[x+b.x+l,y+b.y+t]);
}
function clipObjects(objects,clips){
 if(!clips.length)return objects;
 return objects.flatMap(o=>{
  // Text stays native and editable; this operation clips painted shapes/images.
  if(o.type==='text')return [o];
  const radius=o.geometry==='ellipse'?[[o.w/2,o.h/2]]: [[o.radius||0,o.radius||0]];
  const source=o.paths||(o.clip?[{points:o.clip,close:true}]:[{points:roundedPoints(o.w,o.h,Array(4).fill(radius[0])),close:true}]);
  let paths=source.map(p=>({...p,points:p.points.map(([x,y])=>[x+o.x,y+o.y])}));
  const before=JSON.stringify(paths);
  for(const clip of clips)paths=paths.flatMap(p=>{
   if(p.close){const points=intersectPolygon(p.points,clip);return points.length>=3?[{...p,points}]:[];}
   return p.points.slice(1).flatMap((q,i)=>{const points=intersectLine(p.points[i],q,clip);return points?[{close:false,points}]:[];});
  });
  if(!paths.length)return [];
  if(JSON.stringify(paths)===before)return [o];
  paths=paths.map(p=>({...p,points:p.points.map(([x,y])=>[x-o.x,y-o.y])}));
  return [{...o,...(o.type==='image'?{clip:paths[0].points}:{paths})}];
 });
}
function presetGeometry(o){
 return `<a:prstGeom prst="${o.geometry||'rect'}"><a:avLst>${o.radius&&o.geometry==='roundRect'?`<a:gd name="adj" fmla="val ${Math.round(100000*o.radius/Math.min(o.w,o.h))}"/>`:''}</a:avLst></a:prstGeom>`;
}
function customGeometry(paths,w,h){
 return `<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst>${paths.map(p=>`<a:path w="${Math.max(1,emu(w))}" h="${Math.max(1,emu(h))}"><a:moveTo><a:pt x="${emu(p.points[0][0])}" y="${emu(p.points[0][1])}"/></a:moveTo>${p.points.slice(1).map(([x,y])=>`<a:lnTo><a:pt x="${emu(x)}" y="${emu(y)}"/></a:lnTo>`).join('')}${p.close?'<a:close/>':''}</a:path>`).join('')}</a:pathLst></a:custGeom>`;
}
function shapeXML(o,id){
 const geometry=o.paths?customGeometry(o.paths,o.w,o.h):presetGeometry(o);
 return `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${esc(o.name||'Editable shape')}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>${transform(o)}${geometry}${fillXML(o.fill)}${strokeXML(o.stroke)}</p:spPr></p:sp>`;
}
function textXML(o,id){
 const props=`<a:rPr lang="ru-RU" sz="${Math.round(o.fontSize*60)}" b="${o.bold?1:0}" i="${o.italic?1:0}"${o.spacing?` spc="${Math.round(o.spacing*60)}"`:''}>${solid(o.color)}<a:latin typeface="${esc(o.font)}"/><a:ea typeface="${esc(o.font)}"/><a:cs typeface="${esc(o.font)}"/></a:rPr>`;
 // A wrapped paragraph stays one editable box: the browser's line breaks become
 // explicit breaks and the CSS line pitch becomes exact line spacing.
 const lines=o.lines||[o.text],spacing=o.pitch?`<a:lnSpc><a:spcPts val="${Math.round(o.pitch*60)}"/></a:lnSpc>`:'';
 const runs=lines.map(text=>`<a:r>${props}<a:t xml:space="preserve">${esc(text)}</a:t></a:r>`).join(`<a:br>${props}</a:br>`);
 return `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${esc(o.text.slice(0,65))}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>${transform(o)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>${strokeXML(null)}</p:spPr><p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="ctr"><a:noAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr algn="${o.align||'l'}">${spacing}</a:pPr>${runs}<a:endParaRPr lang="ru-RU"/></a:p></p:txBody></p:sp>`;
}
function pictureXML(o,id,rid){
 const crop=o.crop?`<a:srcRect l="${Math.round(o.crop.l*100000)}" t="${Math.round(o.crop.t*100000)}" r="${Math.round(o.crop.r*100000)}" b="${Math.round(o.crop.b*100000)}"/>`:'';
 const geometry=o.clip?customGeometry([{points:o.clip,close:true}],o.w,o.h):o.paths?customGeometry(o.paths,o.w,o.h):presetGeometry(o);
 return `<p:pic><p:nvPicPr><p:cNvPr id="${id}" name="${esc(o.name||'Original photo')}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="${rid}">${o.grayscale?'<a:grayscl/>':''}${o.alpha<1?`<a:alphaModFix amt="${Math.round(o.alpha*100000)}"/>`:''}</a:blip>${crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr>${transform(o)}${geometry}</p:spPr></p:pic>`;
}
function slideXML(objects,bg,addImage){
 let id=2;
 const shapes=objects.map(o=>{const current=id++;return o.type==='text'?textXML(o,current):o.type==='image'?pictureXML(o,current,addImage(o)):shapeXML(o,current);}).join('');
 return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="${NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:bg><p:bgPr>${solid(bg)}<a:effectLst/></p:bgPr></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>${shapes}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>`;
}
function crc32(bytes){let crc=0xffffffff;for(const b of bytes){crc^=b;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}
function zipStore(files){
 const parts=[],central=[];let offset=0;
 const header=(n)=>{const a=new Uint8Array(n);return [a,new DataView(a.buffer)];};
 for(const [name,data] of Object.entries(files)){
  const n=utf8(name),crc=crc32(data),[local,v]=header(30+n.length);
  v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(6,0x800,true);v.setUint16(12,33,true);v.setUint32(14,crc,true);v.setUint32(18,data.length,true);v.setUint32(22,data.length,true);v.setUint16(26,n.length,true);local.set(n,30);
  parts.push(local,data);
  const [record,r]=header(46+n.length);r.setUint32(0,0x02014b50,true);r.setUint16(4,20,true);r.setUint16(6,20,true);r.setUint16(8,0x800,true);r.setUint16(14,33,true);r.setUint32(16,crc,true);r.setUint32(20,data.length,true);r.setUint32(24,data.length,true);r.setUint16(28,n.length,true);r.setUint32(42,offset,true);record.set(n,46);central.push(record);offset+=local.length+data.length;
 }
 const centralLength=central.reduce((sum,x)=>sum+x.length,0),[end,e]=header(22);
 e.setUint32(0,0x06054b50,true);e.setUint16(8,central.length,true);e.setUint16(10,central.length,true);e.setUint32(12,centralLength,true);e.setUint32(16,offset,true);
 const all=[...parts,...central,end],result=new Uint8Array(all.reduce((sum,x)=>sum+x.length,0));let at=0;for(const x of all){result.set(x,at);at+=x.length;}return result;
}
function buildPptx(slides,template){
 const files=Object.fromEntries(Object.entries(template).map(([k,v])=>[k,decode(v)]));
 const counters=[],mediaCache=new Map();
 slides.forEach((slide,index)=>{
  let rels='<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout7.xml"/>';
  let imageCount=0;
  const xml=slideXML(slide.objects,slide.background,o=>{
   imageCount++;const rid=`rId${imageCount+1}`,m=o.data.match(/^data:([^;]+);base64,(.+)$/s);
   if(!m)throw Error('Only embedded local images are supported');
   const ext=m[1]==='image/png'?'png':'jpeg';let name=mediaCache.get(o.data);
   if(!name){name=`html-asset-${mediaCache.size+1}.${ext}`;mediaCache.set(o.data,name);files[`ppt/media/${name}`]=decode(m[2]);}rels+=`<Relationship Id="${rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/${name}"/>`;return rid;
  });
  files[`ppt/slides/slide${index+1}.xml`]=utf8(xml);
  files[`ppt/slides/_rels/slide${index+1}.xml.rels`]=utf8(`<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${rels}</Relationships>`);
  counters.push({slide:index+1,text:slide.objects.filter(o=>o.type==='text').length,shapes:slide.objects.filter(o=>o.type==='shape').length,images:imageCount});
 });
 let ct=new TextDecoder().decode(files['[Content_Types].xml']);
 for(const [ext,mime] of [['png','image/png'],['jpeg','image/jpeg']])if(!ct.includes(`Extension="${ext}"`))ct=ct.replace('</Types>',`<Default Extension="${ext}" ContentType="${mime}"/></Types>`);
 files['[Content_Types].xml']=utf8(ct);
 return {bytes:zipStore(files),counters};
}
function rect(el,root){const r=el.getBoundingClientRect();return{x:r.x-root.x,y:r.y-root.y,w:r.width,h:r.height};}
function svgFill(value){
 if(value&&value.startsWith('url(')){
  const id=value.match(/#([^)'"\s]+)/)?.[1],el=id&&document.getElementById(id);
  if(el){const stops=Array.from(el.querySelectorAll('stop')).map(s=>{const c=getComputedStyle(s),offset=s.getAttribute('offset')||'0';let rgb=color(c.stopColor);if(rgb)rgb.alpha*=parseFloat(c.stopOpacity||'1');return{position:parseFloat(offset)/(offset.includes('%')?100:1),color:rgb};});
   const coord=(n,d)=>{const s=el.getAttribute(n);return s===null?d:parseFloat(s)/(s.includes('%')?100:1);};
   const angle=90+Math.atan2(coord('y2',0)-coord('y1',0),coord('x2',1)-coord('x1',0))*180/Math.PI;
   if(stops.length)return{stops,angle};}
 }
 return color(value);
}
function svgObjects(svg,root){
 const objects=[];
 for(const el of svg.querySelectorAll('path,polygon,polyline,line,circle,ellipse,rect')){
  if(el.closest('defs'))continue;
  const c=getComputedStyle(el);if(c.display==='none'||c.visibility==='hidden')continue;
  const matrix=el.getScreenCTM();if(!matrix)continue;
  const map=p=>[matrix.a*p.x+matrix.c*p.y+matrix.e-root.x,matrix.b*p.x+matrix.d*p.y+matrix.f-root.y];
  const box=rect(el,root),stroke={color:color(c.stroke),width:parseFloat(c.strokeWidth)*Math.hypot(matrix.a,matrix.b),dash:c.strokeDasharray!=='none'};
  const fill=svgFill(c.fill),alpha=opacityOf(el);
  stroke.cap=c.strokeLinecap==='round'?'rnd':c.strokeLinecap==='square'?'sq':'flat';
  if(stroke.color)stroke.color.alpha*=Number(c.strokeOpacity||1)*alpha;
  for(const v of fill?.stops?fill.stops.map(s=>s.color):[fill])if(v)v.alpha*=Number(c.fillOpacity||1)*alpha;
  // A donut made with dasharray is a collection of arcs, not a dashed ellipse.
  // Materialize its exact painted intervals as editable native paths.
  if(stroke.color&&stroke.dash&&typeof el.getTotalLength==='function'){
   const total=el.getTotalLength(),scale=el.hasAttribute('pathLength')?total/Number(el.getAttribute('pathLength')):1;
   let pattern=c.strokeDasharray.split(/[ ,]+/).map(x=>parseFloat(x)*scale);
   if(pattern.length%2)pattern=pattern.concat(pattern);
   const period=pattern.reduce((a,b)=>a+b,0),paths=[];
   if(period>0){
    let phase=((parseFloat(c.strokeDashoffset)*scale)%period+period)%period,index=0,t=0,guard=0;
    while(phase>=pattern[index]){phase-=pattern[index];index=(index+1)%pattern.length;}
    while(t<total&&guard++<10000){
     const length=Math.min(pattern[index]-phase,total-t);
     if(index%2===0&&length>0){
      const count=Math.min(700,Math.max(2,Math.ceil(length/1.2)));
      paths.push({close:false,points:Array.from({length:count+1},(_,i)=>{const p=map(el.getPointAtLength(t+length*i/count));return[p[0]-box.x,p[1]-box.y];})});
     }
     t+=length;phase=0;index=(index+1)%pattern.length;
    }
   }
   if(paths.length){
    if(fill&&fill.alpha!==0&&(el.tagName==='circle'||el.tagName==='ellipse'))objects.push({type:'shape',...box,geometry:'ellipse',fill});
    objects.push({type:'shape',...box,paths,fill:null,stroke:{...stroke,dash:false},name:'Editable SVG dash segments'});
    continue;
   }
  }
  if(el.tagName==='circle'||el.tagName==='ellipse'){objects.push({type:'shape',...box,geometry:'ellipse',fill,stroke,name:'Editable SVG ellipse'});continue;}
  if(el.tagName==='rect'){const radius=Math.min(Number(el.getAttribute('rx')||0)*Math.hypot(matrix.a,matrix.b),Math.min(box.w,box.h)/2);objects.push({type:'shape',...box,fill,stroke,radius,geometry:radius?'roundRect':'rect',name:'Editable SVG rectangle'});continue;}
  const total=el.getTotalLength();if(!total)continue;
  const count=Math.min(700,Math.max(2,Math.ceil(total/1.2))),points=[];
  for(let i=0;i<=count;i++)points.push(map(el.getPointAtLength(total*i/count)));
  // Separate discontinuous SVG subpaths so unrelated strokes aren't joined.
  const paths=[];let current=[];const maxJump=Math.max(3,total/count*4)*Math.hypot(matrix.a,matrix.b);
  for(const p of points){if(current.length&&Math.hypot(p[0]-current.at(-1)[0],p[1]-current.at(-1)[1])>maxJump){if(current.length>1)paths.push(current);current=[];}current.push(p);}
  if(current.length>1)paths.push(current);
  if(!paths.length)continue;
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),x=Math.min(...xs),y=Math.min(...ys),w=Math.max(...xs)-x,h=Math.max(...ys)-y;
  objects.push({type:'shape',x,y,w:Math.max(w,.01),h:Math.max(h,.01),paths:paths.map(ps=>({points:ps.map(([px,py])=>[px-x,py-y]),close:el.tagName==='polygon'||(!stroke.color&&!!fill)})),fill,stroke,name:'Editable SVG path'});
 }
 for(const text of svg.querySelectorAll('text')){
  const walker=document.createTreeWalker(text,NodeFilter.SHOW_TEXT);let node;
  while((node=walker.nextNode()))objects.push(...textObjects(node,root));
 }
 return objects;
}
function textObjects(node,root){
 if(!node.textContent.trim())return [];
 const el=node.parentElement,c=getComputedStyle(el),objects=[];
 const svgMatrix=el instanceof SVGElement?el.getScreenCTM():null;
 const fontScale=svgMatrix?Math.hypot(svgMatrix.a,svgMatrix.b):1;
 let lines=[],line=null;
 for(let i=0;i<node.length;i++){
  const range=document.createRange();range.setStart(node,i);range.setEnd(node,i+1);const r=range.getBoundingClientRect();
  if(!r.height||!r.width){if(line&&/\s/.test(node.textContent[i]))line.text+=' ';continue;}
  if(!line||Math.abs(line.y-r.y)>2){
   // A browser break between two letters splits a word (overflow-wrap), a layout defect.
   const broken=!!line&&/[\p{L}\p{N}]/u.test(node.textContent[i-1]||'')&&/[\p{L}\p{N}]/u.test(node.textContent[i]);
   line={text:'',x:r.x,y:r.y,right:r.right,h:r.height,broken};lines.push(line);}
  line.text+=node.textContent[i];line.right=Math.max(line.right,r.right);
 }
 const font=c.fontFamily.split(',')[0].replace(/["']/g,'');
 const base={type:'text',fontSize:parseFloat(c.fontSize)*fontScale,font:/system|-apple|BlinkMac/i.test(font)?'Arial':font,bold:parseInt(c.fontWeight)>=600,italic:c.fontStyle==='italic',color:svgMatrix?(color(c.fill)||color(c.color)):color(c.color),spacing:parseFloat(c.letterSpacing)||0};
 const cased=s=>c.textTransform==='uppercase'?s.toUpperCase():c.textTransform==='lowercase'?s.toLowerCase():s;
 const kept=lines.filter(l=>l.text.trim()).map(l=>({...l,text:cased(l.text.replace(/\s+/g,' '))}));
 const align=/center/.test(c.textAlign)?'ctr':/right|end/.test(c.textAlign)?'r':'l';
 // Lines join one box only when they share the paragraph edge. The first line of a run
 // that continues after inline text (bold label, highlight) starts mid-line and stays apart.
 const edge=l=>align==='l'?l.x:align==='r'?l.right:(l.x+l.right)/2;
 const groups=[];
 for(const l of kept){const g=groups.at(-1);if(g&&!svgMatrix&&Math.abs(edge(g.at(-1))-edge(l))<=2)g.push(l);else groups.push([l]);}
 for(const g of groups){
  const broken=g.some(l=>l.broken);
  if(g.length===1){const l=g[0];objects.push({...base,text:l.text,x:l.x-root.x,y:l.y-root.y,w:l.right-l.x+5,h:l.h,broken});continue;}
  const left=Math.min(...g.map(l=>l.x)),right=Math.max(...g.map(l=>l.right)),first=g[0],last=g.at(-1);
  const pitch=(last.y-first.y)/(g.length-1),top=first.y-(pitch-first.h)/2,texts=g.map(l=>l.text.trim());
  objects.push({...base,text:texts.join(' '),lines:texts,pitch,align,x:left-root.x,y:top-root.y,w:right-left+5,h:pitch*g.length,broken});
 }
 return applyOpacity(objects,opacityOf(el));
}
function elementBackground(el,root,pseudo){
 const c=getComputedStyle(el,pseudo||null),box=rect(el,root);
 if(pseudo){if(c.content==='none'||c.content==='normal')return [];box.w=parseFloat(c.width)||box.w;box.h=parseFloat(c.height)||box.h;box.x+=parseFloat(c.left)||0;box.y+=parseFloat(c.top)||0;}
 const fill=gradient(c.backgroundImage)||color(c.backgroundColor),sides=['Top','Right','Bottom','Left'];
 const uniform=sides.every(s=>c[`border${s}Width`]===c.borderTopWidth&&c[`border${s}Color`]===c.borderTopColor);
 const border=uniform?(parseFloat(c.borderTopWidth)||0):0,objects=[];
 const clip=clipPoints(c.clipPath,box.w,box.h);
 if(fill||border){
  objects.push({type:'shape',...box,fill,...cssGeometry(c,box),...(clip?{paths:[{points:clip,close:true}]}:{}),stroke:border?{color:color(c.borderTopColor),width:border}:null,name:pseudo?'Editable decoration':`Editable ${el.className||el.tagName}`});}
 if(!uniform){
  for(const [side,points] of [['Top',[0,0,box.w,0]],['Left',[0,0,0,box.h]],['Right',[box.w,0,box.w,box.h]],['Bottom',[0,box.h,box.w,box.h]]]){
   const width=parseFloat(c[`border${side}Width`])||0;if(!width)continue;
   if(width===border&&c[`border${side}Color`]===c.borderTopColor)continue;
   const [x1,y1,x2,y2]=points;
   objects.push({type:'shape',...box,fill:null,paths:[{points:[[x1,y1],[x2,y2]],close:false}],stroke:{color:color(c[`border${side}Color`]),width},name:'Editable accent border'});
  }
 }
 return applyOpacity(objects,opacityOf(el)*(pseudo?Number(c.opacity):1));
}
function visibleBackground(element){
 let current=element;
 while(current){const c=color(getComputedStyle(current).backgroundColor);if(c&&c.alpha>0.99)return c;current=current.parentElement;}
 return {hex:'FFFFFF',alpha:1};
}
function measureComposition(slide,root){
 const candidates=[];
 for(const el of slide.querySelectorAll('*')){
  if(el.closest('svg')||['STYLE','SCRIPT','IMG','H1','H2','H3'].includes(el.tagName))continue;
  const graph=el.closest('[data-infographic]');
  if(graph&&graph.dataset.infographic===slide.dataset.visualType&&['process','timeline','matrix','before_after','comparison','bar_chart','stack_map','network','table'].includes(slide.dataset.visualType))continue;
  const c=getComputedStyle(el),b=rect(el,root),text=el.textContent.trim();
  if(c.display==='none'||c.visibility==='hidden'||opacityOf(el)<.1||text.length<25||b.w<W*.18||b.h<H*.09)continue;
  const fill=color(c.backgroundColor),paint=(fill&&fill.alpha>=.3)||!!gradient(c.backgroundImage);
  const border=['Top','Right','Bottom','Left'].filter(s=>parseFloat(c[`border${s}Width`])>0&&color(c[`border${s}Color`])?.alpha>0).length>=3;
  if(!paint&&!border)continue;
  // An image frame/caption is not a text card. Connected diagram nodes,
  // data bars and table cells are excluded through their infographic container.
  const imageArea=[...el.querySelectorAll('img')].reduce((sum,i)=>{const r=rect(i,root);return sum+r.w*r.h;},0);
  if(imageArea>b.w*b.h*.25)continue;
  candidates.push({el,box:b});
 }
 const panels=candidates.filter(p=>!candidates.some(q=>q!==p&&q.el.contains(p.el)));
 return {slide:Number(slide.dataset.slide)||null,visualType:slide.dataset.visualType||null,form:slide.dataset.form||null,
  panelCount:panels.length,panelAreaRatio:Math.round(Math.min(1,panels.reduce((sum,p)=>sum+p.box.w*p.box.h,0)/(W*H))*1000)/1000,
  panels:panels.map(p=>Object.fromEntries(Object.entries(p.box).map(([k,v])=>[k,Math.round(v)])))};
}
function measureLayout(slide,objects,number){
 // Free area: below the title (largest text in the upper third) to the bottom margin.
 const texts=objects.filter(o=>o.type==='text'),largest=Math.max(0,...texts.map(o=>o.fontSize));
 const titles=texts.filter(o=>o.fontSize>=largest-.5&&o.y<H*.35);
 const top=titles.length?Math.max(...titles.map(o=>o.y+o.h))+H*.02:H*.08,bottom=H*.94;
 const smallest=Math.min(...texts.map(o=>o.fontSize));
 const body=objects.filter(o=>!titles.includes(o)&&o.name!=='Original preset raster background'&&o.y+o.h>top&&o.y<bottom&&o.w*o.h>16
  &&!(o.type==='text'&&o.fontSize<=smallest+.5&&o.y>bottom-H*.12)&&!(o.w>W*.9&&o.h>H*.5));
 if(!body.length)return {slide:number,visualType:slide.dataset.visualType,region:{y:top,h:bottom-top},content:null};
 const y0=Math.max(top,Math.min(...body.map(o=>o.y))),y1=Math.min(bottom,Math.max(...body.map(o=>o.y+o.h)));
 return {slide:number,visualType:slide.dataset.visualType,region:{y:Math.round(top),h:Math.round(bottom-top)},content:{y:Math.round(y0),h:Math.round(Math.max(0,y1-y0))}};
}
// A word wider than its block: shrink that element's font (not below the minimum or 75 % of
// the original). Words are never split: wrapping inside a word is disabled for the whole deck.
const FIT_MARGIN=1.03; // Target after shrinking: PowerPoint sets text slightly wider than Chrome.
function contentWidth(el){const c=getComputedStyle(el);return el.clientWidth-parseFloat(c.paddingLeft)-parseFloat(c.paddingRight);}
function widestWord(el){
 let width=0,word='';
 for(const n of el.childNodes){
  if(n.nodeType!==3)continue;
  for(const m of n.textContent.matchAll(/\S+/g)){
   const r=document.createRange();r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);
   const w=r.getBoundingClientRect().width;if(w>width){width=w;word=m[0];}
  }
 }
 return [width,word];
}
function fitWords(slide,minFont){
 const fitted=[],failed=[];
 const holders=Array.from(slide.querySelectorAll('*')).filter(el=>!el.closest('svg')&&Array.from(el.childNodes).some(n=>n.nodeType===3&&n.textContent.trim()));
 for(const el of holders){
  const c=getComputedStyle(el);if(c.display==='none'||c.visibility==='hidden'||c.whiteSpace.startsWith('nowrap')||c.whiteSpace==='pre')continue;
  let box=el;while(box!==slide&&getComputedStyle(box).display.startsWith('inline')&&box.parentElement)box=box.parentElement;
  let avail=contentWidth(box);
  // Flex and grid items grow to their longest word; the parent's width is the real limit.
  const parent=box.parentElement;
  if(box!==slide&&parent&&/flex|grid/.test(getComputedStyle(parent).display))avail=Math.min(avail,contentWidth(parent));
  if(avail<=0)continue;
  let [w,word]=widestWord(el);if(w<=avail+.5)continue; // shrink-to-fit boxes are exactly as wide as their text
  const original=parseFloat(c.fontSize),floor=Math.max(minFont,original*.75);let size=original;
  for(let i=0;i<4&&w*FIT_MARGIN>avail&&size>floor+.1;i++){
   size=Math.max(floor,size*avail/(w*FIT_MARGIN));el.style.setProperty('font-size',size.toFixed(2)+'px','important');
   [w]=widestWord(el);
  }
  (w<=avail+.5?fitted:failed).push([word.slice(0,40),Math.round(original),Math.round(size)]);
 }
 return {fitted,failed};
}
async function collectSlides(options={}){
 normalizeFonts();
 if(!document.getElementById('pg-no-word-split')){
  const s=document.createElement('style');s.id='pg-no-word-split';
  s.textContent='section.slide,section.slide *{overflow-wrap:normal!important;word-wrap:normal!important;word-break:normal!important;hyphens:manual!important}';
  document.head.appendChild(s);
 }
 const minFont=options.minFont??20*W/1600;
 await document.fonts.ready;
 await Promise.all(Array.from(document.images).map(i=>i.decode().catch(()=>{})));
 const slides=Array.from(document.querySelectorAll('section.slide')),viewport=document.getElementById('presentation-viewport');
 if(slides.length!==(globalThis.POWERGUIDE_EXPORT_CONFIG?.slideCount||5))throw Error('Unexpected slide count');
 const active=slides.find(s=>getComputedStyle(s).display!=='none')||slides[0];
 const slideDisplay=getComputedStyle(active).display==='none'?'block':getComputedStyle(active).display;
 const nodes=[viewport,...slides].filter(Boolean),saved=nodes.map(el=>[el,el.getAttribute('style')]);
 try{
  if(viewport)viewport.style.cssText+=`;transform:none!important;position:relative!important;top:0!important;left:0!important;width:${W}px!important;height:auto!important;`;
  for(const slide of slides)slide.style.cssText+=`;display:${slideDisplay}!important;width:${W}px!important;height:${H}px!important;transform:none!important;flex-shrink:0!important;`;
  const results=[];
  for(const slide of slides){
   const fit=fitWords(slide,minFont);
   const root=slide.getBoundingClientRect(),objects=[];
   const bg=getComputedStyle(slide).backgroundImage;
   const raster=bg.match(/^url\(["']?(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)["']?\)$/);
   if(raster){const img=new Image();img.src=raster[1];await img.decode();objects.push({type:'image',x:0,y:0,w:W,h:H,data:raster[1],crop:coverCrop(img.naturalWidth,img.naturalHeight,W,H),name:'Original preset raster background'});}
   function walk(el,clips=[]){
    const c=getComputedStyle(el);if(c.display==='none'||c.visibility==='hidden'||c.opacity==='0'||['STYLE','SCRIPT'].includes(el.tagName))return;
    if(el.tagName.toLowerCase()==='svg'){objects.push(...clipObjects(svgObjects(el,root),clips));return;}
    objects.push(...clipObjects(elementBackground(el,root),clips));
    const contour=overflowContour(el,root),b=rect(el,root),pathClip=clipPoints(c.clipPath,b.w,b.h);
    const childClips=[...clips,...(contour?[contour]:[]),...(pathClip?[pathClip.map(([x,y])=>[x+b.x,y+b.y])]:[])];
    objects.push(...clipObjects(elementBackground(el,root,'::before'),childClips));
    if(el.tagName==='IMG'){
     const box=rect(el,root),ir=el.naturalWidth/el.naturalHeight,br=box.w/box.h;let crop={l:0,t:0,r:0,b:0};
     if(c.objectFit==='cover'){if(ir>br)crop.l=crop.r=(1-br/ir)/2;else crop.t=crop.b=(1-ir/br)/2;}
     if(c.objectFit==='contain'){if(ir>br){const h=box.w/ir;box.y+=(box.h-h)/2;box.h=h;}else{const w=box.h*ir;box.x+=(box.w-w)/2;box.w=w;}}
     const original=rect(el,root),own=cssGeometry(c,original);
     const ownClip=clipPoints(c.clipPath,original.w,original.h)||own.paths?.[0].points||roundedPoints(original.w,original.h,cssRadii(c,original.w,original.h));
     const imageClips=[...clips,ownClip.map(([x,y])=>[x+original.x,y+original.y])];
     objects.push(...clipObjects([{type:'image',...box,data:el.src,crop,name:el.alt,alpha:opacityOf(el),grayscale:/grayscale\((1|100%)\)/.test(c.filter)}],imageClips));return;
    }
    // Preserve positioned z-index ordering within each container.
    const children=Array.from(el.childNodes).sort((a,b)=>(a.nodeType===1?parseInt(getComputedStyle(a).zIndex)||0:0)-(b.nodeType===1?parseInt(getComputedStyle(b).zIndex)||0:0));
    for(const node of children){if(node.nodeType===3)objects.push(...textObjects(node,root));else if(node.nodeType===1)walk(node,childClips);}
   }
   walk(slide);
   results.push({background:visibleBackground(slide),objects,fit,composition:measureComposition(slide,root),layout:measureLayout(slide,objects,results.length+1)});
  }
  return results;
 }finally{for(const [el,style] of saved){if(style===null)el.removeAttribute('style');else el.setAttribute('style',style);}}
}
function download(data,name,type){const blob=new Blob([data],{type}),a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
globalThis.PowerGuidePptx={buildPptx,collectSlides,zipStore,shapeXML,textXML,pictureXML,clipPoints,color,coverCrop,roundedPoints,intersectPolygon,clipObjects};
if(typeof document!=='undefined'){
 normalizeFonts();
 const button=document.createElement('button');button.textContent='Скачать редактируемый PPTX';button.style.cssText='position:fixed;top:16px;right:20px;z-index:10000;border:0;border-radius:8px;background:#0E5FC4;color:white;padding:14px 22px;font:600 16px Arial;cursor:pointer;box-shadow:0 3px 12px #0002';
 button.id='export-editable-pptx';document.body.appendChild(button);
 const printStyle=document.createElement('style');printStyle.textContent='@media print{#export-editable-pptx{display:none!important}}';document.head.appendChild(printStyle);
 button.onclick=async()=>{button.disabled=true;button.textContent='Собираю PPTX…';try{const scene=await collectSlides(),result=buildPptx(scene,globalThis.POWERGUIDE_PPTX_TEMPLATE);download(result.bytes,globalThis.POWERGUIDE_EXPORT_CONFIG?.fileName||'presentation-editable.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation');button.textContent='PPTX скачан · повторить';console.table(result.counters);globalThis.POWERGUIDE_LAST_EXPORT={scene,counters:result.counters};}catch(error){console.error(error);button.textContent='Ошибка экспорта — см. консоль';alert('Не удалось создать PPTX: '+error.message);}finally{button.disabled=false;}};
 if(location.hash==='#export-pptx')setTimeout(()=>button.click(),500);
}
})();

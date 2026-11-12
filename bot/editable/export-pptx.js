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
 return `<a:ln w="${emu(stroke.width||1)}" cap="rnd">${solid(stroke.color)}${stroke.dash?'<a:prstDash val="dash"/>':''}<a:round/></a:ln>`;
}
function transform(o){return `<a:xfrm><a:off x="${emu(o.x)}" y="${emu(o.y)}"/><a:ext cx="${Math.max(1,emu(o.w))}" cy="${Math.max(1,emu(o.h))}"/></a:xfrm>`;}
function customGeometry(paths,w,h){
 return `<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst>${paths.map(p=>`<a:path w="${Math.max(1,emu(w))}" h="${Math.max(1,emu(h))}"><a:moveTo><a:pt x="${emu(p.points[0][0])}" y="${emu(p.points[0][1])}"/></a:moveTo>${p.points.slice(1).map(([x,y])=>`<a:lnTo><a:pt x="${emu(x)}" y="${emu(y)}"/></a:lnTo>`).join('')}${p.close?'<a:close/>':''}</a:path>`).join('')}</a:pathLst></a:custGeom>`;
}
function shapeXML(o,id){
 const geometry=o.paths?customGeometry(o.paths,o.w,o.h):`<a:prstGeom prst="${o.geometry||'rect'}"><a:avLst>${o.radius?`<a:gd name="adj" fmla="val ${Math.round(100000*o.radius/Math.min(o.w,o.h))}"/>`:''}</a:avLst></a:prstGeom>`;
 return `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${esc(o.name||'Editable shape')}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>${transform(o)}${geometry}${fillXML(o.fill)}${strokeXML(o.stroke)}</p:spPr></p:sp>`;
}
function textXML(o,id){
 return `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${esc(o.text.slice(0,65))}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>${transform(o)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>${strokeXML(null)}</p:spPr><p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="ctr"><a:noAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr algn="l"/><a:r><a:rPr lang="ru-RU" sz="${Math.round(o.fontSize*60)}" b="${o.bold?1:0}" i="${o.italic?1:0}"${o.spacing?` spc="${Math.round(o.spacing*60)}"`:''}>${solid(o.color)}<a:latin typeface="${esc(o.font)}"/><a:ea typeface="${esc(o.font)}"/><a:cs typeface="${esc(o.font)}"/></a:rPr><a:t xml:space="preserve">${esc(o.text)}</a:t></a:r><a:endParaRPr lang="ru-RU"/></a:p></p:txBody></p:sp>`;
}
function pictureXML(o,id,rid){
 const crop=o.crop?`<a:srcRect l="${Math.round(o.crop.l*100000)}" t="${Math.round(o.crop.t*100000)}" r="${Math.round(o.crop.r*100000)}" b="${Math.round(o.crop.b*100000)}"/>`:'';
 const geometry=o.clip?customGeometry([{points:o.clip,close:true}],o.w,o.h):`<a:prstGeom prst="${o.geometry||'rect'}"><a:avLst/></a:prstGeom>`;
 return `<p:pic><p:nvPicPr><p:cNvPr id="${id}" name="${esc(o.name||'Original photo')}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="${rid}">${o.grayscale?'<a:grayscl/>':''}</a:blip>${crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr>${transform(o)}${geometry}</p:spPr></p:pic>`;
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
  if(el){const stops=Array.from(el.querySelectorAll('stop')).map(s=>{const c=getComputedStyle(s),offset=s.getAttribute('offset')||'0';let rgb=color(c.stopColor);if(rgb)rgb.alpha*=parseFloat(c.stopOpacity||'1');return{position:parseFloat(offset)/(offset.includes('%')?100:1),color:rgb};});if(stops.length)return{stops,angle:135};}
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
  if(el.tagName==='circle'||el.tagName==='ellipse'){objects.push({type:'shape',...box,geometry:'ellipse',fill:svgFill(c.fill),stroke,name:'Editable SVG ellipse'});continue;}
  if(el.tagName==='rect'){objects.push({type:'shape',...box,fill:svgFill(c.fill),stroke,name:'Editable SVG rectangle'});continue;}
  const total=el.getTotalLength();if(!total)continue;
  const count=Math.min(700,Math.max(2,Math.ceil(total/1.2))),points=[];
  for(let i=0;i<=count;i++)points.push(map(el.getPointAtLength(total*i/count)));
  // Separate discontinuous SVG subpaths so unrelated strokes aren't joined.
  const paths=[];let current=[];const maxJump=Math.max(3,total/count*4)*Math.hypot(matrix.a,matrix.b);
  for(const p of points){if(current.length&&Math.hypot(p[0]-current.at(-1)[0],p[1]-current.at(-1)[1])>maxJump){if(current.length>1)paths.push(current);current=[];}current.push(p);}
  if(current.length>1)paths.push(current);
  if(!paths.length)continue;
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),x=Math.min(...xs),y=Math.min(...ys),w=Math.max(...xs)-x,h=Math.max(...ys)-y;
  objects.push({type:'shape',x,y,w:Math.max(w,.01),h:Math.max(h,.01),paths:paths.map(ps=>({points:ps.map(([px,py])=>[px-x,py-y]),close:el.tagName==='polygon'||(!stroke.color&&!!svgFill(c.fill))})),fill:svgFill(c.fill),stroke,name:'Editable SVG path'});
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
  if(!line||Math.abs(line.y-r.y)>2){line={text:'',x:r.x,y:r.y,right:r.right,h:r.height};lines.push(line);}
  line.text+=node.textContent[i];line.right=Math.max(line.right,r.right);
 }
 for(const line of lines){if(!line.text.trim())continue;let text=line.text.replace(/\s+/g,' ');if(c.textTransform==='uppercase')text=text.toUpperCase();if(c.textTransform==='lowercase')text=text.toLowerCase();
  const font=c.fontFamily.split(',')[0].replace(/["']/g,'');
  objects.push({type:'text',text,x:line.x-root.x,y:line.y-root.y,w:line.right-line.x+5,h:line.h,fontSize:parseFloat(c.fontSize)*fontScale,font:/system|-apple|BlinkMac/i.test(font)?'Arial':font,bold:parseInt(c.fontWeight)>=600,italic:c.fontStyle==='italic',color:svgMatrix?(color(c.fill)||color(c.color)):color(c.color),spacing:parseFloat(c.letterSpacing)||0});
 }
 return objects;
}
function elementBackground(el,root,pseudo){
 const c=getComputedStyle(el,pseudo||null),box=rect(el,root);
 if(pseudo){if(c.content==='none'||c.content==='normal')return [];box.w=parseFloat(c.width)||box.w;box.h=parseFloat(c.height)||box.h;box.x+=parseFloat(c.left)||0;box.y+=parseFloat(c.top)||0;}
 const fill=gradient(c.backgroundImage)||color(c.backgroundColor),border=parseFloat(c.borderTopWidth)||0,objects=[];
 const clip=clipPoints(c.clipPath,box.w,box.h);
 if(fill||border){const radius=Math.min(measureValue(c.borderRadius.split('/')[0].split(' ')[0],Math.min(box.w,box.h)),Math.min(box.w,box.h)/2);
  objects.push({type:'shape',...box,fill,geometry:c.borderRadius.includes('%')&&parseFloat(c.borderRadius)>=50?'ellipse':radius?'roundRect':'rect',radius,paths:clip?[{points:clip,close:true}]:null,stroke:border?{color:color(c.borderTopColor),width:border}:null,name:pseudo?'Editable decoration':`Editable ${el.className||el.tagName}`});}
 if(!pseudo){
  for(const [side,points] of [['Left',[0,0,0,box.h]],['Right',[box.w,0,box.w,box.h]],['Bottom',[0,box.h,box.w,box.h]]]){
   const width=parseFloat(c[`border${side}Width`])||0;if(!width)continue;
   if(width===border&&c[`border${side}Color`]===c.borderTopColor)continue;
   const [x1,y1,x2,y2]=points;
   objects.push({type:'shape',...box,fill:null,paths:[{points:[[x1,y1],[x2,y2]],close:false}],stroke:{color:color(c[`border${side}Color`]),width},name:'Editable accent border'});
  }
 }
 return objects;
}
function visibleBackground(element){
 let current=element;
 while(current){const c=color(getComputedStyle(current).backgroundColor);if(c&&c.alpha>0.99)return c;current=current.parentElement;}
 return {hex:'FFFFFF',alpha:1};
}
async function collectSlides(){
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
   const root=slide.getBoundingClientRect(),objects=[];
   const bg=getComputedStyle(slide).backgroundImage;
   const raster=bg.match(/^url\(["']?(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)["']?\)$/);
   if(raster)objects.push({type:'image',x:0,y:0,w:W,h:H,data:raster[1],name:'Original preset raster background'});
   function walk(el){
    const c=getComputedStyle(el);if(c.display==='none'||c.visibility==='hidden'||c.opacity==='0'||['STYLE','SCRIPT'].includes(el.tagName))return;
    if(el.tagName.toLowerCase()==='svg'){objects.push(...svgObjects(el,root));return;}
    objects.push(...elementBackground(el,root));objects.push(...elementBackground(el,root,'::before'));
    if(el.tagName==='IMG'){
     const box=rect(el,root),ir=el.naturalWidth/el.naturalHeight,br=box.w/box.h;let crop={l:0,t:0,r:0,b:0};
     if(c.objectFit==='cover'){if(ir>br)crop.l=crop.r=(1-br/ir)/2;else crop.t=crop.b=(1-ir/br)/2;}
     if(c.objectFit==='contain'){if(ir>br){const h=box.w/ir;box.y+=(box.h-h)/2;box.h=h;}else{const w=box.h*ir;box.x+=(box.w-w)/2;box.w=w;}}
     let clip=null;const parent=el.parentElement,pc=getComputedStyle(parent),pb=rect(parent,root);
     if(Math.abs(pb.w-box.w)<4&&Math.abs(pb.h-box.h)<4)clip=clipPoints(pc.clipPath,box.w,box.h);
     const radius=measureValue(pc.borderRadius.split('/')[0].split(' ')[0],Math.min(box.w,box.h));
     const geometry=radius>0?(pc.borderRadius.includes('%')&&parseFloat(pc.borderRadius)>=50?'ellipse':'roundRect'):'rect';
     objects.push({type:'image',...box,data:el.src,crop,clip,geometry,name:el.alt,grayscale:/grayscale\((1|100%)\)/.test(c.filter)});return;
    }
    // Preserve positioned z-index ordering within each container.
    const children=Array.from(el.childNodes).sort((a,b)=>(a.nodeType===1?parseInt(getComputedStyle(a).zIndex)||0:0)-(b.nodeType===1?parseInt(getComputedStyle(b).zIndex)||0:0));
    for(const node of children){if(node.nodeType===3)objects.push(...textObjects(node,root));else if(node.nodeType===1)walk(node);}
   }
   walk(slide);
   results.push({background:visibleBackground(slide),objects});
  }
  return results;
 }finally{for(const [el,style] of saved){if(style===null)el.removeAttribute('style');else el.setAttribute('style',style);}}
}
function download(data,name,type){const blob=new Blob([data],{type}),a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
globalThis.PowerGuidePptx={buildPptx,collectSlides,zipStore,shapeXML,textXML,pictureXML,clipPoints,color};
if(typeof document!=='undefined'){
 const button=document.createElement('button');button.textContent='Скачать редактируемый PPTX';button.style.cssText='position:fixed;top:16px;right:20px;z-index:10000;border:0;border-radius:8px;background:#0E5FC4;color:white;padding:14px 22px;font:600 16px Arial;cursor:pointer;box-shadow:0 3px 12px #0002';
 button.id='export-editable-pptx';document.body.appendChild(button);
 const printStyle=document.createElement('style');printStyle.textContent='@media print{#export-editable-pptx{display:none!important}}';document.head.appendChild(printStyle);
 button.onclick=async()=>{button.disabled=true;button.textContent='Собираю PPTX…';try{const scene=await collectSlides(),result=buildPptx(scene,globalThis.POWERGUIDE_PPTX_TEMPLATE);download(result.bytes,globalThis.POWERGUIDE_EXPORT_CONFIG?.fileName||'presentation-editable.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation');button.textContent='PPTX скачан · повторить';console.table(result.counters);globalThis.POWERGUIDE_LAST_EXPORT={scene,counters:result.counters};}catch(error){console.error(error);button.textContent='Ошибка экспорта — см. консоль';alert('Не удалось создать PPTX: '+error.message);}finally{button.disabled=false;}};
 if(location.hash==='#export-pptx')setTimeout(()=>button.click(),500);
}
})();

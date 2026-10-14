/** ColorBrewer Blues/Oranges, Cynthia Brewer et al., Apache 2.0. https://colorbrewer2.org/ */
export const BLUES=['#f7fbff','#deebf7','#c6dbef','#9ecae1','#6baed6','#4292c6','#2171b5','#08519c','#08306b'];
export const ORANGES=['#fff5eb','#fee6ce','#fdd0a2','#fdae6b','#fd8d3c','#f16913','#d94801','#a63603','#7f2704'];
export const NO_DATA='#d5dbe2';
export const ZERO='#f1f3f6';
export function adaptiveNumber(value){if(value==null||!Number.isFinite(value))return '—';const a=Math.abs(value);const digits=a>0&&a<.01?Math.min(20,Math.ceil(-Math.log10(a))+1):2;return new Intl.NumberFormat('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:digits}).format(value===0?0:value);}
export function scaleMax(values){return Math.max(0,...values.filter(v=>v!=null&&Number.isFinite(v)).map(Math.abs));}
export function colorValue(value,max,palette=BLUES,diverging=false){if(value==null||!Number.isFinite(value))return NO_DATA;if(value===0)return ZERO;const colors=diverging?(value>0?BLUES:ORANGES):palette;const t=max>0?Math.min(Math.abs(value)/max,1):0;const pos=t*(colors.length-1),i=Math.min(Math.floor(pos),colors.length-2),f=pos-i;const rgb=h=>[1,3,5].map(n=>parseInt(h.slice(n,n+2),16));const a=rgb(colors[i]),b=rgb(colors[i+1]);return `rgb(${a.map((n,k)=>Math.round(n+(b[k]-n)*f)).join(',')})`;}

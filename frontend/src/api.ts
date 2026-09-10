export async function api<T>(path:string, init?:RequestInit):Promise<T>{const r=await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...init});if(!r.ok){let m=r.statusText;try{m=(await r.json()).detail||m}catch{}throw new Error(m)}return r.json()}

export async function apiFile(path:string):Promise<{blob:Blob;filename?:string}>{
 const r=await fetch('/api'+path);
 if(!r.ok){let message=r.statusText;try{message=(await r.json()).detail||message}catch{}throw new Error(message)}
 const disposition=r.headers.get('Content-Disposition')||'';
 const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
 const quoted=disposition.match(/filename="([^"]+)"/i)?.[1];
 let filename:string|undefined;
 try{filename=encoded?decodeURIComponent(encoded):quoted}catch{filename=quoted}
 if(filename)filename=filename.replace(/[\\/\u0000-\u001f\u007f]/g,'-');
 return {blob:await r.blob(),filename};
}

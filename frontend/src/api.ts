export async function api<T>(path:string, init?:RequestInit):Promise<T>{const r=await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...init});if(!r.ok){let m=r.statusText;try{m=(await r.json()).detail||m}catch{}throw new Error(m)}return r.json()}


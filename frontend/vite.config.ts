import {defineConfig} from 'vite'; import react from '@vitejs/plugin-react';
const backend=`http://127.0.0.1:${process.env.BACKEND_PORT||'8000'}`;
export default defineConfig({plugins:[react()],server:{port:Number(process.env.FRONTEND_PORT||3000),proxy:{'/api':backend}}});

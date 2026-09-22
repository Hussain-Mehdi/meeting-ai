import {defineConfig} from 'vite'; import react from '@vitejs/plugin-react'; import tailwind from '@tailwindcss/vite';
const backend=`http://127.0.0.1:${process.env.BACKEND_PORT||'8000'}`;
export default defineConfig({plugins:[react(),tailwind()],server:{port:Number(process.env.FRONTEND_PORT||3000),proxy:{'/api':backend}}});

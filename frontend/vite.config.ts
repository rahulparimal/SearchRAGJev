import { defineConfig } from 'vite'; import react from '@vitejs/plugin-react';
// API_PORT/WEB_PORT come from the shared .env (./rag up picks free ports and writes them there); run_local.sh exports them
// before starting npm so a non-default port never requires editing this file.
const apiPort = process.env.API_PORT || '8000'; const webPort = Number(process.env.WEB_PORT || 5173);
export default defineConfig({plugins:[react()],server:{port:webPort,proxy:{'/api':{target:`http://localhost:${apiPort}`,rewrite:p=>p.replace(/^\/api/,'')}}}});

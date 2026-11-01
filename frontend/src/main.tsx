import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App.tsx'
import './index.css'

// 管理后台按需加载：antd 只进 /admin 的 chunk，用户工作台的加载体积不因
// 管理界面的存在而变重（付费用户走 /，运营商只有一人）。
const AdminApp = lazy(() => import('./admin/AdminApp.tsx'))

// 同一 SPA、同一构建产物（spec #24 决策）：/admin 路径树渲染管理后台外壳，
// 其余路径仍是用户工作台（App 行为不变）。/admin 深链刷新由 Nginx 的 SPA
// fallback（try_files → /index.html）落到这里，不会 404。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route
          path="/admin/*"
          element={
            <Suspense fallback={null}>
              <AdminApp />
            </Suspense>
          }
        />
        <Route path="*" element={<App />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)

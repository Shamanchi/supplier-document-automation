import { Button, Layout, Space, Spin, Typography } from 'antd'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import './App.css'
import { useAuth } from './auth/AuthContext'
import LoginPage from './auth/LoginPage'
import { isNavigationActive } from './navigation'
import DocumentPage from './pages/DocumentPage'
import DocumentsPage from './pages/DocumentsPage'
import RegistryImportsPage from './pages/RegistryImportsPage'
import RegistryPage from './pages/RegistryPage'
import ReviewPage from './pages/ReviewPage'
import UploadPage from './pages/UploadPage'

const { Header, Content } = Layout

export default function App() {
  const { token, user, isLoading, logout } = useAuth()
  const { pathname } = useLocation()

  if (isLoading) {
    return (
      <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <Spin size="large" />
      </Layout>
    )
  }
  if (!token) return <LoginPage />
  if (!user?.roles.includes('sysadmin')) {
    return <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}><Typography.Text type="danger">Доступ к инженерному интерфейсу разрешён только системному администратору.</Typography.Text></Layout>
  }

  const navItems = [
    { href: '/', label: 'Документы', active: isNavigationActive(pathname, '/') },
    { href: '/review', label: 'Подтверждение', active: isNavigationActive(pathname, '/review') },
    { href: '/upload', label: 'Загрузка', active: isNavigationActive(pathname, '/upload') },
    { href: '/registry', label: 'Реестр', active: isNavigationActive(pathname, '/registry') },
    {
      href: '/registry-imports',
      label: 'Файлы реестра',
      active: isNavigationActive(pathname, '/registry-imports'),
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        className="app-header"
        style={{ background: '#16324F', display: 'flex', alignItems: 'center', paddingInline: 24 }}
      >
        <Typography.Title className="app-title" level={4} style={{ color: '#fff', margin: 0, flex: 1 }}>
          DocPrice · Сертификаты и реестр
        </Typography.Title>
        <Space className="app-nav" size="small" wrap>
          {navItems.map((item) => (
            <Link
              key={item.href}
              to={item.href}
              className={`app-nav-link${item.active ? ' is-active' : ''}`}
            >
              <Button type={item.active ? 'default' : 'text'} style={{ color: item.active ? undefined : '#fff' }}>
                {item.label}
              </Button>
            </Link>
          ))}
          <Button type="text" style={{ color: '#fff' }} onClick={logout}>
            Выйти
          </Button>
        </Space>
      </Header>
      <Content className="app-content" style={{ padding: 24 }}>
        <div className="app-shell">
          <Routes>
            <Route path="/" element={<DocumentsPage />} />
            <Route path="/documents/:id" element={<DocumentPage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/upload" element={<UploadPage />} />
            <Route path="/registry" element={<RegistryPage />} />
            <Route path="/registry-imports" element={<RegistryImportsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </Content>
    </Layout>
  )
}

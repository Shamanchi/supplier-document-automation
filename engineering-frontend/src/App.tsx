import { Alert, Button, Layout, Space, Spin, Tag, Typography } from 'antd';
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import LoginPage from './auth/LoginPage';
import ImportPage from './pages/ImportPage';
import ImportedDataPage from './pages/ImportedDataPage';
import ImportsPage from './pages/ImportsPage';
import CategoriesPage from './pages/CategoriesPage';
import PlaygroundPage from './pages/PlaygroundPage';
import ReviewQueuePage from './pages/ReviewQueuePage';
import RulesPage from './pages/RulesPage';
import RuleEditorPage from './pages/RuleEditorPage';
import ApiAccessPage from './pages/ApiAccessPage';
import './App.css';
import { isNavigationActive } from './navigation';
import { ProjectProvider } from './projects/ProjectContext';
import { useProject } from './projects/ProjectContext';
import ProjectSelector from './projects/ProjectSelector';

const { Header, Content } = Layout;

function AppLayout() {
  const { user, isAdmin, logout } = useAuth();
  const { pathname } = useLocation();
  const navItems = [
    { href: '/', label: 'Прайсы', active: isNavigationActive(pathname, '/') },
    { href: '/data', label: 'Позиции', active: isNavigationActive(pathname, '/data') },
    { href: '/review-queue', label: 'Проверка', active: isNavigationActive(pathname, '/review-queue') },
    { href: '/playground', label: 'Подбор', active: isNavigationActive(pathname, '/playground') },
    { href: '/categories', label: 'Категории', active: isNavigationActive(pathname, '/categories') },
    { href: '/rules', label: 'Правила', active: isNavigationActive(pathname, '/rules') },
    ...(isAdmin ? [{ href: '/api-access', label: 'API', active: isNavigationActive(pathname, '/api-access') }] : []),
  ];
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        className="app-header"
        style={{
          background: '#16324F',
          display: 'flex',
          alignItems: 'center',
          paddingInline: 24,
        }}
      >
        <Typography.Title className="app-title" level={4} style={{ color: '#fff', margin: 0, flex: 1 }}>
          DocPrice · Импорт прайсов
        </Typography.Title>
        <Space className="app-nav" wrap>
          <ProjectSelector isAdmin={isAdmin} />
          {navItems.map((item) => (
            <Link
              key={item.href}
              to={item.href}
              className={`app-nav-link${item.active ? ' is-active' : ''}`}
              aria-current={item.active ? 'page' : undefined}
            >
              <Button ghost size="small">{item.label}</Button>
            </Link>
          ))}
          <Typography.Text style={{ color: '#fff' }}>{user?.username}</Typography.Text>
          <Tag color={isAdmin ? 'gold' : 'blue'}>{isAdmin ? 'admin' : 'user · чтение'}</Tag>
          <Button ghost size="small" onClick={() => void logout()}>
            Выйти
          </Button>
        </Space>
      </Header>
      <Content className="app-content" style={{ padding: 24, background: '#f5f7fa' }}>
        <div
          className="app-shell"
          style={{
            background: '#fff',
            borderRadius: 8,
            padding: 24,
            minHeight: 'calc(100vh - 112px)',
          }}
        >
          <Routes>
            <Route path="/" element={<ImportsPage />} />
            <Route path="/imports/:id" element={<ImportPage />} />
            <Route path="/data" element={<ImportedDataPage />} />
            <Route path="/review-queue" element={<ReviewQueuePage />} />
            <Route path="/playground" element={<PlaygroundPage />} />
            <Route path="/categories" element={<CategoriesPage />} />
            <Route path="/rules" element={<RulesPage />} />
            <Route path="/rules/:ruleId" element={<RuleEditorPage />} />
            <Route path="/api-access" element={isAdmin ? <ApiAccessPage /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </Content>
    </Layout>
  );
}

function AuthGate() {
  const { user, isLoading, isSysadmin, logout } = useAuth();
  if (isLoading) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!user) return <LoginPage />;
  if (!isSysadmin) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24 }}>
        <Alert
          type="error"
          showIcon
          message="Доступ запрещён"
          description="Инженерный интерфейс доступен только системному администратору."
          action={<Button onClick={() => void logout()}>Выйти</Button>}
        />
      </div>
    );
  }
  return <ProjectProvider><ProjectBoundApp /></ProjectProvider>;
}

function ProjectBoundApp() {
  const { project } = useProject();
  return <AppLayout key={project.id} />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter basename="/engineering">
        <AuthGate />
      </BrowserRouter>
    </AuthProvider>
  );
}

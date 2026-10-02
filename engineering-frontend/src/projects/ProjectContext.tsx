import { useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Spin } from 'antd';
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { createProject as createProjectRequest, extractApiError, fetchProjects, setProjectId } from '../api/client';
import type { Project } from '../api/types';

interface ProjectContextValue {
  projects: Project[];
  project: Project;
  selectProject: (projectId: string) => void;
  createProject: (input: { slug: string; name: string; description?: string }) => Promise<void>;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);
const PROJECT_KEY = 'price-loader-project-id';

export function ProjectProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string>();

  const activate = (next: Project) => {
    setProjectId(next.id);
    window.localStorage.setItem(PROJECT_KEY, next.id);
    setProject(next);
    queryClient.clear();
  };

  const load = () => {
    setError(undefined);
    fetchProjects().then(
      (rows) => {
        setProjects(rows);
        const remembered = window.localStorage.getItem(PROJECT_KEY);
        const selected = rows.find((row) => row.id === remembered)
          ?? rows.find((row) => row.is_default)
          ?? rows[0];
        if (selected) activate(selected);
        else setError('Не найдено ни одного проекта');
      },
      // Без этой ветки провайдер навсегда оставался в состоянии «проекта нет» и
      // отдавал `null`: любая ошибка списка проектов означала пустую страницу
      // без единого сообщения о причине.
      (reason) => setError(extractApiError(reason)),
    );
  };

  useEffect(load, []);

  const value = useMemo<ProjectContextValue | null>(() => project ? ({
    projects,
    project,
    selectProject: (projectId) => {
      const selected = projects.find((row) => row.id === projectId);
      if (selected) activate(selected);
    },
    createProject: async (input) => {
      const created = await createProjectRequest(input);
      const rows = await fetchProjects();
      setProjects(rows);
      activate(rows.find((row) => row.id === created.id) ?? created);
    },
  }) : null, [project, projects]);

  if (value) return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24 }}>
      {error ? (
        <Alert
          type="error"
          showIcon
          style={{ maxWidth: 520 }}
          message="Не удалось загрузить список проектов"
          description={error}
          action={<Button size="small" onClick={load}>Повторить</Button>}
        />
      ) : (
        <Spin size="large" />
      )}
    </div>
  );
}

export function useProject(): ProjectContextValue {
  const value = useContext(ProjectContext);
  if (!value) throw new Error('useProject must be used inside ProjectProvider');
  return value;
}

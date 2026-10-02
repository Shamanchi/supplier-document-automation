import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useAuth } from "@/features/auth/model";
import { extractApiError, setProjectId } from "@/shared/api/client";
import type { ProjectDto } from "@/shared/api/backend-types";
import { fetchProjects } from "@/shared/api/resources";
import { env } from "@/shared/config/env";
import { Button } from "@/shared/ui/button";
import { ErrorState, LoadingState } from "@/shared/ui/page";

interface ProjectContextValue {
  projects: ProjectDto[];
  project: ProjectDto;
  selectProject: (projectId: string) => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);
const PROJECT_KEY = "price-loader-project-id";

export function ProjectProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const [projects, setProjects] = useState<ProjectDto[]>([]);
  const [project, setProject] = useState<ProjectDto | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const activate = useCallback(
    (nextProject: ProjectDto, clearQueries: boolean) => {
      setProjectId(nextProject.id);
      window.localStorage.setItem(PROJECT_KEY, nextProject.id);
      setProject(nextProject);
      if (clearQueries) queryClient.clear();
    },
    [queryClient],
  );

  const load = useCallback(async () => {
    setError(null);
    setIsLoading(true);
    try {
      const rows = await fetchProjects();
      const remembered = window.localStorage.getItem(PROJECT_KEY);
      const selected =
        rows.find((row) => row.id === remembered) ??
        rows.find((row) => row.is_default) ??
        rows[0];
      if (!selected) {
        setError("На сервере не найдено ни одного проекта");
        return;
      }
      setProjects(rows);
      activate(selected, false);
    } catch (loadError) {
      setError(extractApiError(loadError));
    } finally {
      setIsLoading(false);
    }
  }, [activate]);

  useEffect(() => {
    if (env.enableMsw || !isAuthenticated) {
      setProjectId(null);
      setProjects([]);
      setProject(null);
      return;
    }
    void load();
  }, [isAuthenticated, load]);

  const value = useMemo<ProjectContextValue | null>(
    () =>
      project
        ? {
            projects,
            project,
            selectProject: (projectId) => {
              const selected = projects.find((row) => row.id === projectId);
              if (selected && selected.id !== project.id) activate(selected, true);
            },
          }
        : null,
    [activate, project, projects],
  );

  if (env.enableMsw || !isAuthenticated) return children;
  if (value) {
    return (
      <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
    );
  }
  if (isLoading) return <LoadingState label="Загрузка проектов..." />;
  return (
    <div className="grid min-h-screen place-items-center p-6">
      <div className="grid max-w-lg gap-4 text-center">
        <ErrorState label={error ?? "Не удалось загрузить проекты"} />
        <Button type="button" onClick={() => void load()}>
          Повторить
        </Button>
      </div>
    </div>
  );
}

export function useProject(): ProjectContextValue {
  const context = useContext(ProjectContext);
  if (!context) {
    throw new Error("useProject must be used within ProjectProvider");
  }
  return context;
}

import { MutationCache, QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { extractApiError } from "@/shared/api/client";

// Мутации по всему приложению вызываются как mutateAsync без локального
// catch (успех показывается toast'ом рядом с вызовом). Глобальный onError —
// единственное место, где отказ доходит до пользователя: без него упавшая
// мутация выглядит как «клик без реакции» (требование шага 07: клик не
// роняет приложение, но и не молчит).
//
// Мутация, которая сама показывает ошибку (try/catch вокруг mutateAsync с
// toast.error), помечается meta.suppressGlobalError — иначе пользователь
// видит два одинаковых тоста.
// Мутации, чей отказ показывается локально (try/catch вокруг mutateAsync с
// toast.error в вызывающих страницах/виджетах): глобальный тост по этому
// флагу подавляется — сообщение не дублируется.
export const locallyReportedMutation = {
  suppressGlobalError: true,
} as const;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (mutation.meta?.suppressGlobalError) return;
      toast.error(extractApiError(error));
    },
  }),
});

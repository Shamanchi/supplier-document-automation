import { env } from "@/shared/config/env";

export async function enableMocking() {
  if (!env.enableMsw || import.meta.env.MODE === "test") {
    return;
  }

  const { worker } = await import("@/shared/mocks/browser");
  await worker.start({
    onUnhandledRequest: "bypass",
    serviceWorker: {
      url: "/mockServiceWorker.js",
    },
  });
}

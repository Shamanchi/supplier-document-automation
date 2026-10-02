import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "@/app/app";
import "@/app/styles/index.css";
import { enableMocking } from "@/shared/mocks/enable";

async function bootstrap() {
  await enableMocking();

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

void bootstrap();

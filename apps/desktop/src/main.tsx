import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

document.documentElement.classList.toggle("desktop-floating", window.interviewDesktop?.isElectron === true);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

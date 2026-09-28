import { render } from "preact";
import "./app.css";
import { App } from "./App";
import { applyTheme, readTheme } from "./theme";

// CSP 不允许内联脚本，主题在首次渲染前由入口脚本设置。
applyTheme(readTheme());

const root = document.getElementById("app");
if (root) render(<App />, root);

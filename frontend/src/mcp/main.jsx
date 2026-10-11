// MCP 配置面板入口（FW 第五入口，106）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("mcp-root"));

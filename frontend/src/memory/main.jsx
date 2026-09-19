// 记忆面板入口（FW 新栈第二入口，025）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("memory-root"));

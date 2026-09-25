// 知识语料面板入口（FW 新栈第四入口，042）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("notes-root"));

// 知识图谱面板入口（FW 新栈第三入口，S7b）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("graph-root"));
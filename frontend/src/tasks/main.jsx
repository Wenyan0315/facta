// 任务视图 v2（FW 站试点）入口：把 App 组件挂到容器上——仅此而已。
// 「状态即真相，DOM 是投影」：从此文件之后，没人再手写 querySelector。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("run-list"));

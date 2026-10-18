// 方案评审页入口（FW 第六入口，ADR 111）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("review-root"));

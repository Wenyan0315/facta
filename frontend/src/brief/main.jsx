// 开工 Brief 页入口（FW 第七入口，ADR 113）：挂 App，仅此而已。
import { render } from "preact";
import App from "./App.jsx";

render(<App />, document.getElementById("brief-root"));

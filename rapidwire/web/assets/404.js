// Not-found page: names the requested path, written as text.
import { h, mount } from "/assets/app.js";

mount("missing-path", "404 ", h("code", null, window.location.pathname));

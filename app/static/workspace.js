import { setupCreator } from "./creator.js";
import { setupEditor } from "./editor.js";

const creator = document.getElementById("creator");
const editor = document.getElementById("editor");
const showCreator = document.getElementById("show-create");
const showEditor = document.getElementById("show-editor");
const openEditor = setupEditor();
setupCreator();

function select(editing) {
    creator.hidden = editing;
    editor.hidden = !editing;
    showCreator.setAttribute("aria-pressed", String(!editing));
    showEditor.setAttribute("aria-pressed", String(editing));
    if (editing) openEditor();
}

showCreator.addEventListener("click", () => select(false));
showEditor.addEventListener("click", () => select(true));

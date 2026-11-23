"use strict";

const dropZone = document.querySelector("[data-drop-zone]");
if (dropZone) {
    const input = dropZone.querySelector("input[type=file]");
    const list = dropZone.querySelector("[data-file-list]");
    const renderFiles = () => {
        const names = Array.from(input.files, (file) => {
            const item = document.createElement("li");
            item.textContent = file.name;
            return item;
        });
        list.replaceChildren(...names);
    };

    input.addEventListener("change", renderFiles);
    dropZone.addEventListener("dragover", (event) => {
        event.preventDefault();
        dropZone.classList.add("dragging");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
    dropZone.addEventListener("drop", (event) => {
        event.preventDefault();
        dropZone.classList.remove("dragging");
        if (event.dataTransfer.files.length) {
            input.files = event.dataTransfer.files;
            renderFiles();
        }
    });
}

const forecastForm = document.querySelector("[data-forecast]");
if (forecastForm) {
    const eventList = forecastForm.querySelector("[data-event-list]");
    const template = forecastForm.querySelector("[data-event-template]");
    const addButton = forecastForm.querySelector("[data-add-event]");
    const delaySelect = forecastForm.querySelector("[data-delay-event]");
    let nextKey = 0;
    const keysInUse = () => Array.from(eventList.querySelectorAll("[name=event_key]"), (field) => field.value);

    const updateDelayChoices = () => {
        const selected = delaySelect.value;
        const options = [new Option("Keep income dates", "")];
        for (const row of eventList.children) {
            const fields = (name) => row.querySelector(`[name=event_${name}]`).value.trim();
            const amount = Number(fields("amount").replace(/[$,]/g, ""));
            if (amount > 0) {
                const label = fields("name") || "Planned income";
                const date = fields("date");
                options.push(new Option(date ? `${label} · ${date}` : label, fields("key")));
            }
        }
        delaySelect.replaceChildren(...options);
        delaySelect.value = options.some((option) => option.value === selected) ? selected : "";
        addButton.disabled = eventList.children.length >= 12;
    };

    eventList.addEventListener("input", updateDelayChoices);
    eventList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-remove-event]");
        if (button) {
            button.closest("[data-event-row]").remove();
            addButton.focus();
            updateDelayChoices();
        }
    });
    addButton.addEventListener("click", () => {
        if (eventList.children.length >= 12) return;
        while (keysInUse().includes(String(nextKey))) nextKey += 1;
        const draft = template.content.cloneNode(true);
        draft.querySelector("[name=event_key]").value = String(nextKey);
        const input = draft.querySelector("[name=event_name]");
        eventList.append(draft);
        input.focus();
        updateDelayChoices();
    });
    updateDelayChoices();
}

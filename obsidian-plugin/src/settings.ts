import { App, PluginSettingTab, Setting } from "obsidian";
import type KrunRagPlugin from "./main";

export interface KrunRagSettings {
  serverUrl: string;
  topK: number;
  rerankerEnabled: boolean;
  noAnalyze: boolean;
  maxChunksPerFile: number;
  sendActiveNote: boolean;
}

export const DEFAULT_SETTINGS: KrunRagSettings = {
  serverUrl: "http://127.0.0.1:8765",
  topK: 8,
  rerankerEnabled: false,
  noAnalyze: false,
  maxChunksPerFile: 2,
  sendActiveNote: false,
};

export class KrunRagSettingTab extends PluginSettingTab {
  plugin: KrunRagPlugin;

  constructor(app: App, plugin: KrunRagPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "KRUN RAG settings" });

    new Setting(containerEl)
      .setName("Server URL")
      .setDesc("FastAPI server endpoint. Default localhost:8765.")
      .addText((t) =>
        t
          .setPlaceholder("http://127.0.0.1:8765")
          .setValue(this.plugin.settings.serverUrl)
          .onChange(async (v) => {
            this.plugin.settings.serverUrl = v.trim().replace(/\/$/, "");
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Top-K citations")
      .setDesc("Max number of chunks sent to Claude (default 8).")
      .addSlider((s) =>
        s
          .setLimits(3, 20, 1)
          .setValue(this.plugin.settings.topK)
          .setDynamicTooltip()
          .onChange(async (v) => {
            this.plugin.settings.topK = v;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Max chunks per file")
      .setDesc("Diversification: cap chunks per single file in top-K.")
      .addSlider((s) =>
        s
          .setLimits(1, 5, 1)
          .setValue(this.plugin.settings.maxChunksPerFile)
          .setDynamicTooltip()
          .onChange(async (v) => {
            this.plugin.settings.maxChunksPerFile = v;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Enable reranker")
      .setDesc("Use bge-reranker-v2-m3 (slower, higher recall). Requires phase1d extra on the server.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.rerankerEnabled).onChange(async (v) => {
          this.plugin.settings.rerankerEnabled = v;
          await this.plugin.saveSettings();
        }),
      );

    new Setting(containerEl)
      .setName("Skip query analyzer")
      .setDesc("Bypass Haiku filter extraction; faster but no metadata WHERE clause.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.noAnalyze).onChange(async (v) => {
          this.plugin.settings.noAnalyze = v;
          await this.plugin.saveSettings();
        }),
      );

    new Setting(containerEl)
      .setName("Send active note as context hint")
      .setDesc("OFF (default): always search the entire vault. ON: prepend the open note's name to the query for 'this note' focus.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.sendActiveNote).onChange(async (v) => {
          this.plugin.settings.sendActiveNote = v;
          await this.plugin.saveSettings();
        }),
      );
  }
}

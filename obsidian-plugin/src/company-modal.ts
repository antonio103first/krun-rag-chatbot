import { App, SuggestModal } from "obsidian";
import { CompanyEntry } from "./api";

/** Fuzzy-ish picker over companies present in the index.
 *
 * The list comes from the server (`/companies`) rather than from vault folder
 * names, because the briefing filters on the `company` metadata field — a
 * folder whose notes never got that field set would offer a choice that
 * returns nothing.
 */
export class CompanyPickerModal extends SuggestModal<CompanyEntry> {
  constructor(
    app: App,
    private entries: CompanyEntry[],
    private onPick: (company: string) => void,
  ) {
    super(app);
    this.setPlaceholder("회사명을 입력하세요 (미팅 이력이 있는 회사만)");
  }

  getSuggestions(query: string): CompanyEntry[] {
    const q = query.trim().toLowerCase();
    if (!q) return this.entries;
    return this.entries.filter((e) => e.company.toLowerCase().includes(q));
  }

  renderSuggestion(entry: CompanyEntry, el: HTMLElement): void {
    el.createDiv({ text: entry.company });
    el.createEl("small", { text: `노트 ${entry.notes}건` });
  }

  onChooseSuggestion(entry: CompanyEntry): void {
    this.onPick(entry.company);
  }
}

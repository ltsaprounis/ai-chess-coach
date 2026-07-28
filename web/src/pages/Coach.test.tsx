import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { describe, expect, it } from "vitest";
import { adviceMarkdownComponents } from "./Coach";

// The backend mints advice citations as reference-style links whose
// definitions are app-relative game deep links where the game id
// embeds a colon (`uuid:username`), e.g. `/games/abc-123:leo?ply=25`
// (docs/06-coach.md "Game links"). react-markdown's default
// `urlTransform` treats a colon appearing after the first `/` as part
// of the path rather than a protocol, so it should pass such an href
// through untouched — verified here rather than assumed, alongside
// the new-tab override `adviceMarkdownComponents` adds
// (docs/08-frontend.md, Coach page item 5).
describe("adviceMarkdownComponents", () => {
  it("renders a game citation link with its href intact, opening in a new tab", () => {
    const advice =
      "See [your 26...Nb6 in the June 14 blitz game][g1] for the critical moment.\n\n" +
      "[g1]: /games/abc-123-uuid:leo?ply=25\n";

    const html = renderToStaticMarkup(
      <Markdown components={adviceMarkdownComponents}>{advice}</Markdown>,
    );

    const anchor = html.match(/<a\b[^>]*>[^<]*<\/a>/)?.[0];
    expect(anchor).toBeDefined();
    expect(anchor).toContain('href="/games/abc-123-uuid:leo?ply=25"');
    expect(anchor).toContain('target="_blank"');
    expect(anchor).toContain('rel="noreferrer"');
    expect(anchor).toContain(">your 26...Nb6 in the June 14 blitz game<");
  });
});

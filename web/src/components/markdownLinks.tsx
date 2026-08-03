import type { Components } from "react-markdown";

/**
 * Opens every markdown link in a new tab. Shared by the Coach page's
 * advice (`Coach.tsx`) and the shared `ChatPanel` — both render
 * markdown whose citations are app-relative game deep links
 * (`[text][g1]` + `[g1]: /games/{id}?ply={n}`, docs/06-coach.md "Game
 * links"; chat replies mint the same links from tool results,
 * docs/archive/coach-chat.md). Advice/chat state lives in
 * `useMutation`/`useReducer` state, so a same-tab navigation into a
 * game would blank the panel until the next request
 * (docs/08-frontend.md, Coach page) — open these links in a new tab
 * instead.
 */
export const gameLinkMarkdownComponents: Components = {
  a: ({ node, ...props }) => <a target="_blank" rel="noreferrer" {...props} />,
};

/**
 * The tables named on screen in the landing page's Atlas, keyed by physical table id,
 * with the clean name Stratum gives each. Kept apart from the scene so the page can
 * render these labels without pulling three.js into its main bundle.
 */
export const SCENE_LABELS: Record<string, string> = {
  "public.cstmr": "customer",
  "public.tblFilm": "film",
  "public.invtry": "inventory",
  "public.fin_txn": "fin_txn",
};

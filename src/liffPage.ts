/**
 * liffPage.ts — Rain Route LIFF page
 * Ported from github.com/bewkingston/rain-alert-bot @ c7e89ae (liff/index.html),
 * with the map-picker lat/lon bug fixed (confirmMapLocation() now writes
 * dataset.lat/lon, not just .value — analyze() reads dataset for the request).
 */

import liffHtmlTemplate from "./liff.html";

export function renderLiffPage(liffId: string): string {
  return liffHtmlTemplate.replace(/__LIFF_ID__/g, liffId);
}

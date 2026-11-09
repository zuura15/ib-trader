/** Viewport breakpoint for the mobile (vertical three-tab) layout. */
// Anything narrower than the desktop's side-by-side chart columns gets
// the vertical three-tab layout: phones, portrait foldables (a Galaxy
// Fold's inner screen is ~750–900 CSS px), and a desktop browser window
// snapped to half the screen (~960 px on a 1920 display). The Settings
// "Layout mode" toggle still forces either layout.
export const MOBILE_QUERY = '(max-width: 1100px)';

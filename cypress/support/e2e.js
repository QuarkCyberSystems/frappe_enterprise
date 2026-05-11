// ***********************************************************
// This example support/index.js is processed and
// loaded automatically before your test files.
//
// This is a great place to put global configuration and
// behavior that modifies Cypress.
//
// You can change the location of this file or turn off
// automatically serving support files with the
// 'supportFile' configuration option.
//
// You can read more here:
// https://on.cypress.io/configuration
// ***********************************************************

// Import commands.js using ES2015 syntax:
import "./commands";
import "@cypress/code-coverage/support";

// ERPNext-side custom commands (Badia WP test helpers). Auto-loaded so any
// `**/ui_test_*.js` spec under ERPNext has the helpers available without
// importing per-spec. Falls back silently if ERPNext isn't installed.
try {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	require("../../../erpnext/erpnext/public/js/cypress_commands.js");
} catch (e) {
	// ERPNext not present — Frappe-only Cypress runs proceed unchanged.
}

Cypress.on("uncaught:exception", (err, runnable) => {
	return false;
});

// Alternatively you can use CommonJS syntax:
// require('./commands')

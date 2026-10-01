# Database Migrations
Schema changes ship as versioned migration scripts reviewed like code. Migrations must be backwards compatible so the previous release keeps working. Large index builds use the concurrent option to avoid table locks.

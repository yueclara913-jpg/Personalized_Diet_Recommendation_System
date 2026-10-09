-- Schema v1. Connection must enable foreign_keys before starting a transaction.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS data_sources (
 source_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(source_id)) > 0),
 source_name TEXT NOT NULL CHECK(length(trim(source_name)) > 0),
 organization TEXT NOT NULL,
 official_url TEXT NOT NULL,
 license_id TEXT NOT NULL,
 license_url TEXT,
 redistribution_status TEXT NOT NULL DEFAULT 'unknown' CHECK(redistribution_status IN ('allowed','restricted','unknown'))
);
CREATE TABLE IF NOT EXISTS source_releases (
 release_id TEXT PRIMARY KEY NOT NULL,
 source_id TEXT NOT NULL REFERENCES data_sources(source_id),
 source_version TEXT NOT NULL,
 published_at TEXT,
 retrieved_at TEXT NOT NULL,
 source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256)=64 AND source_file_sha256 NOT GLOB '*[^0-9a-f]*'),
 source_url TEXT NOT NULL,
 UNIQUE(source_id, source_version, source_file_sha256)
);
CREATE TABLE IF NOT EXISTS foods (
 food_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(food_id)) > 0),
 food_name TEXT NOT NULL CHECK(length(trim(food_name)) > 0),
 food_name_en TEXT,
 food_name_zh TEXT,
 category TEXT,
 preparation_state TEXT
);
CREATE TABLE IF NOT EXISTS ocr_imports (
 ocr_import_id TEXT PRIMARY KEY NOT NULL,
 release_id TEXT NOT NULL REFERENCES source_releases(release_id),
 image_source TEXT NOT NULL,
 image_sha256 TEXT NOT NULL CHECK(length(image_sha256)=64 AND image_sha256 NOT GLOB '*[^0-9a-f]*'),
 raw_text TEXT NOT NULL,
 engine_name TEXT NOT NULL,
 engine_version TEXT NOT NULL,
 recognition_confidence REAL CHECK(recognition_confidence IS NULL OR (typeof(recognition_confidence) IN ('integer','real') AND recognition_confidence BETWEEN 0 AND 1)),
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected')),
 imported_at TEXT NOT NULL,
 UNIQUE(ocr_import_id, release_id)
);
CREATE TABLE IF NOT EXISTS food_profiles (
 profile_id TEXT PRIMARY KEY NOT NULL,
 food_id TEXT NOT NULL REFERENCES foods(food_id),
 release_id TEXT NOT NULL REFERENCES source_releases(release_id),
 source_food_id TEXT NOT NULL CHECK(length(trim(source_food_id)) > 0),
 source_data_type TEXT NOT NULL,
 source_locator TEXT NOT NULL,
 nutrition_basis TEXT NOT NULL CHECK(nutrition_basis IN ('per_100g','per_100ml','per_serving','unknown')),
 basis_quantity REAL,
 basis_unit TEXT,
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected')),
 value_origin TEXT NOT NULL CHECK(value_origin IN ('analysis','estimated','label','ocr_label','unknown')),
 ocr_import_id TEXT,
 energy_method TEXT,
 carbohydrate_definition TEXT,
 fiber_definition TEXT,
 mapping_version TEXT NOT NULL,
 FOREIGN KEY(ocr_import_id, release_id) REFERENCES ocr_imports(ocr_import_id, release_id),
 CHECK((value_origin='ocr_label' AND ocr_import_id IS NOT NULL) OR (value_origin<>'ocr_label' AND ocr_import_id IS NULL)),
 CHECK(
  (nutrition_basis='unknown' AND basis_quantity IS NULL AND basis_unit IS NULL) OR
  (nutrition_basis='per_100g' AND basis_quantity IS NOT NULL AND basis_quantity=100 AND basis_unit IS NOT NULL AND basis_unit='g') OR
  (nutrition_basis='per_100ml' AND basis_quantity IS NOT NULL AND basis_quantity=100 AND basis_unit IS NOT NULL AND basis_unit='ml') OR
  (nutrition_basis='per_serving' AND basis_quantity IS NOT NULL AND basis_quantity=1 AND basis_unit IS NOT NULL AND basis_unit='serving')
 ),
 UNIQUE(release_id, source_data_type, source_food_id)
);
CREATE TABLE IF NOT EXISTS nutrient_definitions (
 nutrient_code TEXT PRIMARY KEY NOT NULL,
 nutrient_name TEXT NOT NULL,
 unit TEXT NOT NULL CHECK(unit IN ('kcal','g','mg')),
 definition TEXT NOT NULL,
 UNIQUE(nutrient_code, unit)
);
-- Definitions only: these are NOT food nutrient measurements.
INSERT INTO nutrient_definitions VALUES
 ('calories','能量','kcal','Preserve source energy method'),
 ('protein','蛋白质','g','Preserve source protein definition'),
 ('fat','总脂肪','g','Total fat'),
 ('carbohydrates','碳水化合物','g','Definition recorded on profile'),
 ('fiber','膳食纤维','g','Definition recorded on profile'),
 ('sugar','总糖','g','Total sugars, not added sugars'),
 ('sodium','钠','mg','Sodium, not salt'),
 ('iron','铁','mg','Iron') ON CONFLICT(nutrient_code) DO NOTHING;
CREATE TABLE IF NOT EXISTS food_nutrients (
 profile_id TEXT NOT NULL REFERENCES food_profiles(profile_id),
 nutrient_code TEXT NOT NULL,
 unit TEXT NOT NULL,
 standardized_value REAL CHECK(standardized_value IS NULL OR (typeof(standardized_value) IN ('integer','real') AND standardized_value BETWEEN 0 AND 1e12)),
 raw_value TEXT,
 raw_unit TEXT,
 source_nutrient_id TEXT,
 value_qualifier TEXT NOT NULL CHECK(value_qualifier IN ('measured','estimated','label','trace','below_loq','missing')),
 conversion_rule TEXT,
 CHECK(standardized_value IS NULL OR (raw_value IS NOT NULL AND length(trim(raw_value))>0 AND raw_unit IS NOT NULL AND length(trim(raw_unit))>0 AND conversion_rule IS NOT NULL AND length(trim(conversion_rule))>0)),
 CHECK(value_qualifier<>'missing' OR standardized_value IS NULL),
 FOREIGN KEY(nutrient_code, unit) REFERENCES nutrient_definitions(nutrient_code, unit),
 PRIMARY KEY(profile_id, nutrient_code)
);
CREATE TABLE IF NOT EXISTS food_portions (
 portion_id TEXT PRIMARY KEY NOT NULL,
 profile_id TEXT NOT NULL REFERENCES food_profiles(profile_id),
 portion_name TEXT NOT NULL,
 serving_quantity REAL NOT NULL CHECK(typeof(serving_quantity) IN ('integer','real') AND serving_quantity > 0 AND serving_quantity <= 1e12),
 serving_unit TEXT NOT NULL CHECK(length(trim(serving_unit)) > 0),
 serving_weight_g REAL CHECK(serving_weight_g IS NULL OR (typeof(serving_weight_g) IN ('integer','real') AND serving_weight_g > 0 AND serving_weight_g <= 1e12)),
 source_locator TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))
);
CREATE TABLE IF NOT EXISTS food_aliases (
 alias_id TEXT PRIMARY KEY NOT NULL,
 food_id TEXT NOT NULL REFERENCES foods(food_id),
 alias TEXT NOT NULL CHECK(length(trim(alias)) > 0),
 language TEXT NOT NULL,
 evidence TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected')),
 UNIQUE(food_id, alias, language)
);
CREATE TABLE IF NOT EXISTS food_application_tags (
 tag_id TEXT PRIMARY KEY NOT NULL,
 food_id TEXT NOT NULL REFERENCES foods(food_id),
 tag_name TEXT NOT NULL CHECK(tag_name IN ('meal_type','vegetarian','vegan')),
 tag_value TEXT NOT NULL,
 evidence TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected')),
 CHECK((tag_name='meal_type' AND tag_value IN ('breakfast','lunch','dinner','snack')) OR (tag_name IN ('vegetarian','vegan') AND tag_value IN ('true','false','unknown'))),
 UNIQUE(food_id, tag_name, tag_value)
);
CREATE TABLE IF NOT EXISTS review_events (
 review_event_id TEXT PRIMARY KEY NOT NULL,
 profile_id TEXT REFERENCES food_profiles(profile_id),
 ocr_import_id TEXT REFERENCES ocr_imports(ocr_import_id),
 review_status TEXT NOT NULL CHECK(review_status IN ('pending','approved','rejected')),
 reviewer TEXT NOT NULL,
 reviewed_at TEXT NOT NULL,
 evidence TEXT NOT NULL,
 CHECK((profile_id IS NOT NULL AND ocr_import_id IS NULL) OR (profile_id IS NULL AND ocr_import_id IS NOT NULL))
);
-- Candidates only; completeness, tags and portions still require later validation.
CREATE VIEW IF NOT EXISTS reviewed_food_profiles AS
 SELECT p.* FROM food_profiles p
 WHERE p.review_status='approved' AND p.nutrition_basis<>'unknown'
 AND (p.value_origin<>'ocr_label' OR EXISTS (
  SELECT 1 FROM ocr_imports o WHERE o.ocr_import_id=p.ocr_import_id
  AND o.release_id=p.release_id AND o.review_status='approved'
 ));

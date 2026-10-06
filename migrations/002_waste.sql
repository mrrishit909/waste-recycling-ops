-- Domain model. Names follow the blueprint's schema; additions are marked (+). Points are stored as x/y kilometres on the district
-- grid rather than PostGIS geography: the simulator has a street grid, not a map.

CREATE TABLE districts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, name text NOT NULL, model_seed int NOT NULL, now_hour int NOT NULL, start_date date NOT NULL, created_at timestamptz NOT NULL DEFAULT now());   -- (+) seed and clock
SELECT enable_tenant_rls('districts');

CREATE TABLE sites (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, name text NOT NULL, site_type text NOT NULL);   -- (+)
SELECT enable_tenant_rls('sites');

CREATE TABLE containers (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, site_id uuid NOT NULL REFERENCES sites, stream text NOT NULL, capacity_l numeric NOT NULL, geom_x numeric NOT NULL, geom_y numeric NOT NULL, sensor_id text NOT NULL,
  sim_index int NOT NULL, schedule_every int NOT NULL, schedule_day int NOT NULL, UNIQUE (tenant_id, sensor_id));   -- (+) index, fixed schedule
SELECT enable_tenant_rls('containers');

CREATE TABLE fill_readings (container_id uuid NOT NULL REFERENCES containers, ts timestamptz NOT NULL, fill_pct numeric NOT NULL, temp_c numeric, battery_pct numeric, tenant_id uuid NOT NULL REFERENCES tenants, source text NOT NULL DEFAULT 'gateway',   -- (+) source
  PRIMARY KEY (container_id, ts));
SELECT enable_tenant_rls('fill_readings');

CREATE TABLE vehicles (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, depot_id uuid, capacity_kg numeric NOT NULL, stream_capabilities text[] NOT NULL, shift_start time NOT NULL, shift_end time NOT NULL, name text NOT NULL);   -- (+) name
SELECT enable_tenant_rls('vehicles');

CREATE TABLE pickups (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, container_id uuid NOT NULL REFERENCES containers, vehicle_id uuid REFERENCES vehicles, planned_at timestamptz NOT NULL, completed_at timestamptz, weight_kg numeric,
  plan_id uuid, sequence int, overflowed boolean);   -- (+) which plan, the order on the route, whether the container had overflowed
SELECT enable_tenant_rls('pickups');
CREATE INDEX pickups_planned ON pickups (tenant_id, planned_at);

CREATE TABLE plans (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, kind text NOT NULL, for_day date NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), created_by uuid NOT NULL, summary jsonb NOT NULL, routes jsonb NOT NULL);   -- (+)
SELECT enable_tenant_rls('plans');

CREATE TABLE facilities (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, name text NOT NULL, facility_type text NOT NULL, geom_x numeric NOT NULL, geom_y numeric NOT NULL, accepted_streams text[] NOT NULL, daily_capacity_kg numeric NOT NULL, recovery_yield numeric NOT NULL);   -- (+) yield
SELECT enable_tenant_rls('facilities');

CREATE TABLE deliveries (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, facility_id uuid NOT NULL REFERENCES facilities, for_day date NOT NULL, stream text NOT NULL, weight_kg numeric NOT NULL, vehicle text, decided_by uuid, created_at timestamptz NOT NULL DEFAULT now());   -- (+) what each facility has been sent today
SELECT enable_tenant_rls('deliveries');

CREATE TABLE sort_observations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, facility_id uuid NOT NULL REFERENCES facilities, captured_at timestamptz NOT NULL, class text NOT NULL, confidence numeric NOT NULL, contamination boolean NOT NULL, image_uri text,
  batch_id uuid NOT NULL, model_version text NOT NULL);   -- (+) batch and model
SELECT enable_tenant_rls('sort_observations');

CREATE TABLE material_prices (material text NOT NULL, region text NOT NULL, date date NOT NULL, price_per_ton numeric NOT NULL, tenant_id uuid NOT NULL REFERENCES tenants, PRIMARY KEY (material, region, date, tenant_id));
SELECT enable_tenant_rls('material_prices');

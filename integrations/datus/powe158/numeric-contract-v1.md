# SOLVING CONTRACT (normative; applies to every question in this database)

These rules are the normative solving contract for scoring. Read them before writing SQL; they define required numeric precision, NULL and ordering semantics.

G1 Scope: only birdbench.customers, gasstations, products, transactions_1k and yearmonth. The transaction table is an observed sample, not lifetime behavior. No currency conversion, card ownership inference or economic equivalence of Price and Consumption.

G2 Preserve base-row multiplicity unless explicitly using distinct sets. Ignore NULL numeric values only where stated. Missing and zero differ. A mean over no values is NULL. Counts over empty sets are zero. Dimension joins use primary IDs. Keys not joined to dimensions remain eligible where stated.

G3 Normalize each non-NULL DOUBLE Price/Consumption to DECIMAL(38,6) before comparisons or arithmetic. Ratios must use at least 12 fractional decimal places for intermediate computation (prefer DECIMAL(60,18)); rank before rounding. Floating transcendental operations, regression residuals, Fourier, and covariance are evaluated with sufficient precision; clamp negative variance caused solely by arithmetic roundoff to zero. Final decimals rounded to six places. Zero denominators yield NULL. Decimal6 comparison absolute tolerance 0.0000005, zero relative tolerance; IDs/counts/integers exact. For spectral near-ties apply the explicit question rule, not display rounding.

G4 NULL only equals NULL in outputs. Text grouping and comparison follow stored utf8mb4_bin: case-sensitive, trailing U+0020 spaces ignored in equality; preserve returned labels. All question-required currencies exclude NULL. Output columns and order are normative. SQL ascending places NULL first. Numeric strings in oracle are numeric values. Empty results retain column schema. No implicit LIMIT.

G5 A valid timestamp has non-NULL Date and Time matching ^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$. Interpret as local timestamp without timezone conversion; event order timestamp then TransactionID. Valid YYYYMM matches ^[0-9]{4}(0[1-9]|1[0-2])$ with year 1000..9999; month date is day one. Month-distance means 12*year+month difference. Calendar adjacency never means merely previous observed row. Explicit date ranges are half-open. Date ISO YYYY-MM-DD, timestamp YYYY-MM-DD HH:MM:SS, month YYYYMM.

SELECT
    MASTER_OPS_ROWID,
    FAMILY,
    PART_ID,
    PART_DESCRIPTION,
    PART_TYPE,
    WORK_ORDER,
    WO_REV,
    MAX(TRANSACTION_DATE) AS LAST_TRANSACTION_DATE,
    OPERATION_SEQUENCE,
    OPERATION,
    OPERATION_TYPE,
    OPERATION_STATUS,
    RESOURCE_ID,
    RESOURCE_DESCRIPTION,
    ANALYTIC_SECTION,
    LT_TYPE,
    USER_5,
    -- ADDED: how many different people actually booked on this work order x
    -- step. Against USER_5 - the crew the routing PLANS for - it says whether
    -- the standard describes what happened at all. A 1-person, 1-hour setup
    -- performed by two people is not a fast or slow version of that plan; it
    -- is a different job, and no arithmetic turns it into evidence about the
    -- planned standard.
    -- CHANGED 2026-08-26. Was COUNT(DISTINCT EMPLOYEE_ID) over the inner
    -- rows, which undercounts: the inner GROUP BY carries
    -- LT.TRANSACTION_DATE and EMP.DEPARTMENT_ID, so two people booking the
    -- same day in the same department collapse into ONE inner row and
    -- MIN(LT.EMPLOYEE_ID) keeps one of them. Confirmed against the source
    -- system: a press operation with two run tickets by two operators on the
    -- same shift reported EMP_DISTINCT = 1 and LT_ROWS = 1.
    -- SUM(LT.HOURS_WORKED) was never affected - it aggregates inside the
    -- group - so HOURS_WORKED stayed correct while these two counts did not.
    -- Verified after the change: 31 rows gained a higher EMP_DISTINCT and 84
    -- a higher LT_ROWS, every one an increase, with no row's HOURS_WORKED or
    -- per-key row count moving - so the join cannot have fanned out.
    -- TO REVERT: restore the ORIGINAL line and drop the LT_COUNT join.
    -- ORIGINAL: COUNT(DISTINCT EMPLOYEE_ID) AS EMP_DISTINCT,
    MAX(EMP_DISTINCT_TRUE) AS EMP_DISTINCT,
    -- A GUARD, not a correction. It counts how many times HOURS_MASTER was
    -- REPEATED, which EMP_DISTINCT cannot see: one person booking two tickets
    -- leaves EMP_DISTINCT = USER_5, so the row passes the EMP_DISTINCT filter
    -- while the master was counted twice and the ratio comes out halved.
    --
    -- IT HAPPENS: 98 setup rows in the 13 Aug 2026 extract, and only 25 of
    -- them are also caught by EMP_DISTINCT <> USER_5. So 73 rows would pass a
    -- crew-only filter while still carrying a doubled master and a halved
    -- ratio. Both tests are needed; neither subsumes the other.
    --
    -- An earlier check said this never occurred. That check grouped by
    -- TRANSACTION_DATE, so it only asked "does one person book twice ON THE
    -- SAME DAY" - no. Across different days, over a whole work order x step,
    -- it is common. Do not re-derive it that way.
    --
    -- Run is immune either way: its HOURS_MASTER is built from quantities and
    -- is genuinely additive, never a repeated constant.
    -- CHANGED 2026-08-26, same cause: COUNT(*) counted INNER ROWS, not
    -- labour tickets. Same work order reported LT_ROWS = 1 against two
    -- real tickets.
    -- ORIGINAL: COUNT(*) AS LT_ROWS,
    MAX(LT_ROWS_TRUE) AS LT_ROWS,
    -- ADDED 2026-08-26. 1 where the crew-division exemption fired on any part
    -- of this row's evidence, so HOURS_WORKED is man-hours rather than elapsed
    -- there. Exists to close a blind spot, not to be corrected: if it turns out
    -- to be 0 everywhere the question is answered and the column can go.
    MAX(CREW_EXEMPT) AS CREW_EXEMPT,
    RUN_TYPE_MASTER,
    RUN_MASTER,
    LOAD_SIZE_QTY_MASTER,
    -- ADDED 2026-10-09. The work order's own SETUP_HRS, read from O like
    -- RUN_MASTER above, so setup has a work-order copy of its standard that is
    -- not rebuilt from the tickets. HOURS_MASTER cannot be that copy: the inner
    -- query credits SETUP_HRS / USER_5 once per INNER ROW, and the inner GROUP
    -- BY merges tickets that share TRANSACTION_DATE and DEPARTMENT_ID - the
    -- same collapse the 2026-08-26 note on EMP_DISTINCT describes. Confirmed
    -- against the source system: a crew-3 setup at 0.50 h, three tickets on
    -- one date, two operators in one department and one in another, came out
    -- at HOURS_MASTER 0.3334 (two inner rows) while LT_ROWS said 3.
    -- An AGGREGATE, so it cannot change the grain. MAX because the outer GROUP
    -- BY keys on the work order's base id only, so the lot/split/sub copies of
    -- one operation roll up together.
    -- TO REVERT: delete this line and the marked one in the inner query.
    MAX(SETUP_HRS_MASTER) AS SETUP_HRS_MASTER,
    -- ADDED 2026-09-28. TODAY'S MASTER, beside the work order's copy.
    -- RUN_TYPE_MASTER, RUN_MASTER, LOAD_SIZE_QTY_MASTER and USER_5 above come
    -- from O - the WORK ORDER's own operation copy, i.e. the standard the job
    -- ran under. That is the right yardstick for the test and stays so. But the
    -- update list also used it as "current", and on 2026-09-28 23 of 361
    -- checkable PCS/HR rows showed the master had already moved on (a new
    -- revision, or an edit in place) - e.g. copies at 35 against a master at 90.
    -- These columns read the same fields from O_M, the current master line
    -- MASTER_OPS_ROWID points at, so the list can show both.
    -- NULL wherever MASTER_OPS_ROWID is NULL (no current master line).
    -- AGGREGATES, so they cannot change the grain: O_M is fixed by
    -- MASTER_OPS_ROWID, which is in the GROUP BY, so MAX simply carries it up.
    --
    -- CURRENT_REV is the part's current revision (PART_SITE.ENGINEERING_MSTR),
    -- the revision the line above belongs to. Beside WO_REV it says WHY a
    -- work order's copy can differ from today's master: WO_REV = CURRENT_REV
    -- means the master was edited in place after the order was created;
    -- WO_REV <> CURRENT_REV means the part has moved to another revision.
    -- PART_SITE holds one row per part (checked 2026-09-28 over every M.% part,
    -- max 1 row each), so this is one value per part and cannot fan out.
    --
    -- TO REVERT: delete these six lines and the six marked ones in the inner
    -- query; no other line was touched.
    MAX(RUN_TYPE_MASTER_TODAY) AS RUN_TYPE_MASTER_TODAY,
    MAX(RUN_MASTER_TODAY) AS RUN_MASTER_TODAY,
    MAX(LOAD_SIZE_QTY_MASTER_TODAY) AS LOAD_SIZE_QTY_MASTER_TODAY,
    MAX(SETUP_HRS_MASTER_TODAY) AS SETUP_HRS_MASTER_TODAY,
    MAX(USER_5_MASTER_TODAY) AS USER_5_MASTER_TODAY,
    MAX(CURRENT_REV) AS CURRENT_REV,
    -- AGGREGATES, deliberately, exactly like EMPLOYEE_ID above: a column in the
    -- GROUP BY could split a row in two if the value ever varied within it,
    -- whereas MIN() cannot change the grain - same rows out, two more columns.
    MIN(RUN_HRS) AS RUN_HRS,
    MIN(CALC_START_QTY) AS CALC_START_QTY,
    SUM(TOTAL_QTY) AS TOTAL_QTY,
    SUM(GOOD_QTY) AS GOOD_QTY,
    SUM(BAD_QTY) AS BAD_QTY,
    SUM(HOURS_WORKED) AS HOURS_WORKED,
    CAST(
        CASE
            WHEN LT_TYPE = 'Run'
             AND RTRIM(RUN_TYPE_MASTER) = 'HRS/LOAD'
             AND SUM(GOOD_QTY + BAD_QTY) > 0
            -- Both branches are cast to the SAME type on purpose. Without
            -- it the CASE takes its result type from the multiplication -
            -- RUN_MASTER is NUMERIC(19,2) - and T-SQL then rounds the ELSE
            -- branch to 2 decimals as well, silently coarsening every PCS/HR
            -- row that this expression was never meant to touch.
            THEN CAST(CEILING(
                     SUM(GOOD_QTY + BAD_QTY)
                     / NULLIF(CAST(LOAD_SIZE_QTY_MASTER AS NUMERIC(19, 4)), 0)
                 ) * RUN_MASTER AS NUMERIC(19, 4))
            ELSE CAST(SUM(HOURS_MASTER) AS NUMERIC(19, 4))
        END AS NUMERIC(19, 4)
    ) AS HOURS_MASTER,
    CAST(
        SUM(HOURS_WORKED)
        - CASE
              WHEN LT_TYPE = 'Run'
               AND RTRIM(RUN_TYPE_MASTER) = 'HRS/LOAD'
               AND SUM(GOOD_QTY + BAD_QTY) > 0
              THEN CAST(CEILING(
                       SUM(GOOD_QTY + BAD_QTY)
                       / NULLIF(CAST(LOAD_SIZE_QTY_MASTER AS NUMERIC(19, 4)), 0)
                   ) * RUN_MASTER AS NUMERIC(19, 4))
              ELSE CAST(SUM(HOURS_MASTER) AS NUMERIC(19, 4))
          END
        AS NUMERIC(19, 4)
    ) AS HOURS_DELTA
FROM (
    SELECT
        O_M.ROWID AS MASTER_OPS_ROWID,          -- CHANGED: now pulled from the master work order's operation, not the transactional WO's operation
        LT.WORKORDER_BASE_ID AS WORK_ORDER,  
        WO.USER_8 AS WO_REV,                  
        (
            SELECT STRING_VAL 
            FROM dbo.USER_DEF_FIELDS AS UDF 
            WHERE DOCUMENT_ID = P.ID AND ID = 'UDF-0000026'
        ) AS FAMILY,              
        UDF2.STRING_VAL AS PART_TYPE,
        P.ID AS PART_ID,                      
        P.DESCRIPTION AS PART_DESCRIPTION,    
        LT.TRANSACTION_DATE,                  
        LT.OPERATION_SEQ_NO AS OPERATION_SEQUENCE,  
        O.OPERATION_TYPE AS OPERATION,        
        SR.BID_RATE_CATEGORY_ID AS OPERATION_TYPE,  
        O.STATUS AS OPERATION_STATUS,         
        LT.RESOURCE_ID AS RESOURCE_ID,        
        SR.DESCRIPTION AS RESOURCE_DESCRIPTION, 
        SR.USER_7 AS ANALYTIC_SECTION,   
        CASE
            WHEN LT.TYPE = 'R' THEN 'Run'     
            WHEN LT.TYPE = 'S' THEN 'Setup'   
            ELSE '-'                         
        END AS LT_TYPE,
        O.USER_5 AS USER_5,
        O.RUN_TYPE AS RUN_TYPE_MASTER,
        CAST(O.RUN AS NUMERIC(19, 2)) AS RUN_MASTER,
        CAST(O.LOAD_SIZE_QTY AS int) AS LOAD_SIZE_QTY_MASTER,
        -- ADDED 2026-10-09: the work order's SETUP_HRS, see the outer query.
        -- O.SETUP_HRS is already in this GROUP BY, so the grain cannot move.
        CAST(O.SETUP_HRS AS NUMERIC(19, 4)) AS SETUP_HRS_MASTER,
        -- ADDED 2026-09-28: today's master line, see the outer query. MAX() so
        -- the grain cannot move - O_M.ROWID is in this GROUP BY, so each is one
        -- value per group. Same casts as the work-order columns above.
        MAX(O_M.RUN_TYPE) AS RUN_TYPE_MASTER_TODAY,
        MAX(CAST(O_M.RUN AS NUMERIC(19, 2))) AS RUN_MASTER_TODAY,
        MAX(CAST(O_M.LOAD_SIZE_QTY AS int)) AS LOAD_SIZE_QTY_MASTER_TODAY,
        MAX(CAST(O_M.SETUP_HRS AS NUMERIC(19, 4))) AS SETUP_HRS_MASTER_TODAY,
        MAX(O_M.USER_5) AS USER_5_MASTER_TODAY,
        MAX(PS.ENGINEERING_MSTR) AS CURRENT_REV,
        -- ADDED 2026-08-26: the two fields the PCS/HR master used to be built
        -- from. RUN_HRS is the master time for CALC_START_QTY pieces, so
        -- RUN_HRS / CALC_START_QTY is hours per INPUT piece. Carried out so the
        -- relationship can be checked rather than inferred: on a routing that
        -- preserves the piece count, CALC_START_QTY / RUN_HRS equals O.RUN;
        -- where the operation turns one piece into two it equals O.RUN / 2.
        -- Nothing downstream reads them - they exist to be asserted on.
        CAST(O.RUN_HRS AS NUMERIC(19, 4)) AS RUN_HRS,
        CAST(O.CALC_START_QTY AS NUMERIC(19, 4)) AS CALC_START_QTY,
        CAST(SUM(LT.GOOD_QTY + LT.BAD_QTY) AS INT) AS TOTAL_QTY,  
        CAST(SUM(LT.GOOD_QTY) AS INT) AS GOOD_QTY,                
        CAST(SUM(LT.BAD_QTY) AS INT) AS BAD_QTY,                  
        -- ADDED: an AGGREGATE, so it does not belong in the GROUP BY and
        -- cannot change the grain - same rows out, one more column. A row
        -- holds one employee, so MIN() IS that employee; this exists only to
        -- carry the id up to the outer COUNT(DISTINCT).
        MIN(LT.EMPLOYEE_ID) AS EMPLOYEE_ID,
        -- ADDED 2026-08-26. AGGREGATES, like the MIN above and for the same
        -- reason: they cannot appear in the GROUP BY, so they cannot change
        -- the grain. Both are constant within an inner group, so MAX simply
        -- carries the value up to the outer query.
        MAX(LT_COUNT.EMP_DISTINCT_TRUE) AS EMP_DISTINCT_TRUE,
        MAX(LT_COUNT.LT_ROWS_TRUE)      AS LT_ROWS_TRUE,
        -- ADDED 2026-08-26. Which branch the HOURS_WORKED case below took.
        -- SR.USER_7 reaches the extract as ANALYTIC_SECTION so the section arm
        -- was always testable, but EMP.DEPARTMENT_ID is used in the CASE and in
        -- the GROUP BY and never projected - so nothing downstream could tell
        -- whether a row's hours were divided by the crew or not. This says so
        -- directly, without carrying a personnel attribute into the extract.
        --
        -- An AGGREGATE, so it cannot change the grain. Within an inner group it
        -- is constant (both inputs are in the GROUP BY); across the groups the
        -- outer query rolls up, MAX means "the exemption fired on at least one
        -- ticket group behind this row", i.e. these hours are partly undivided.
        MAX(CASE WHEN SR.USER_7 IN ('LATHING', 'MILLING', 'TBP CELL')
                      OR EMP.DEPARTMENT_ID IN ('MEC', 'Fresatura', 'Stampi', 'Tornitura')
                 THEN 1 ELSE 0 END) AS CREW_EXEMPT,
        -- CHANGED 2026-10-09: the department list gained 'Fresatura', 'Stampi'
        -- and 'Tornitura' beside 'MEC'. Departments were reorganised, and
        -- EMPLOYEE.DEPARTMENT_ID is the employee's department TODAY, applied
        -- to every ticket they ever booked - so the list has to name the new
        -- departments for the rule to keep firing, and keeps 'MEC' for anyone
        -- still filed under it. THE SAME LIST IS IN THREE PLACES - CREW_EXEMPT
        -- above, HOURS_WORKED here and HOURS_DELTA below - and all three must
        -- agree, or a row's hours go undivided without CREW_EXEMPT saying so.
        -- ORIGINAL (all three): EMP.DEPARTMENT_ID IN ('MEC')
        -- calculate the total hours worked based on department or analytic section
        CASE
            WHEN SR.USER_7 IN ('LATHING', 'MILLING', 'TBP CELL') OR EMP.DEPARTMENT_ID IN ('MEC', 'Fresatura', 'Stampi', 'Tornitura') 
            THEN 
                CAST(SUM(ISNULL(LT.HOURS_WORKED, 0)) AS NUMERIC(19, 4)) 
            ELSE 
                CAST(ISNULL(SUM(ISNULL(LT.HOURS_WORKED, 0)) / NULLIF(O.USER_5, 0), SUM(ISNULL(LT.HOURS_WORKED, 0))) AS NUMERIC(19, 4)) 
        END AS HOURS_WORKED, 
        -- calculate the master hours based on the labor ticket type and quantities processed
        CAST(
            CASE
                -- SETUP_HRS is a single ELAPSED value on the master line: 0.5 is
                -- half an hour of setup, whoever does it. The division is
                -- DE-DUPLICATION, not a unit conversion - this inner query emits
                -- one row per labour ticket, so a crew of three repeats the
                -- master value three times before the outer SUM collects it.
                -- Dividing first makes the sum land back on the true value.
                -- The rounding proves it: crew-3 rows come out at 0.5001, being
                -- 0.5/3 = 0.16667 at NUMERIC(19,4) summed three times, where a
                -- stored 1.5 divided by 3 would give exactly 0.5000.
                -- DO NOT REMOVE - without it every crewed setup is multiplied
                -- by its crew size.
                WHEN LT.TYPE = 'S' THEN O.SETUP_HRS / ISNULL(NULLIF(O.USER_5, 0), 1)
                -- for run type labor tickets, calculate run hours based on load size or start quantity
                WHEN LT.TYPE = 'R' THEN
                    CASE 
                        WHEN SUM(GOOD_QTY + LT.BAD_QTY) = 0 THEN 0  
                        ELSE 
                            CASE 
                                -- if hours are calculated per HRS/LOAD
                                WHEN O.RUN_TYPE = 'HRS/LOAD'  
                                THEN CEILING(((SUM(GOOD_QTY + LT.BAD_QTY)) / O.LOAD_SIZE_QTY)) * O.RUN
                                -- ---------------- CHANGED 2026-08-26 ----------------
                                -- TO REVERT: uncomment the ORIGINAL line below and delete the three
                                -- lines after it (WHEN / THEN / ELSE 0).
                                -- WHY. The old line is hours per INPUT piece (RUN_HRS / CALC_START_QTY)
                                -- multiplied by an OUTPUT count. Those are the same number only while the
                                -- operation preserves the piece count. In Press & Cutting the forge turns
                                -- one piece into two, and on 68 part x step routings the master came out
                                -- doubled - measured at exactly 2.0x on 107 of 110 lines, the other three
                                -- short only by scrapped pieces. O.RUN is already stated per OUTPUT piece,
                                -- so the new form needs no knowledge of the input count and is right
                                -- whether or not pieces split, and whether or not any were scrapped.
                                --
                                -- IMPACT, measured on the 2026-08-26 extract: 94.9% of PCS/HR lines move by
                                -- under 1% (a 2-decimal rounding in RUN_HRS disappearing), 116 lines halve,
                                -- nothing else changes. Setup, HRS/LOAD and HRS/PC identical to the hour.
                                --
                                -- The old ELSE was a CATCH-ALL and also covered HRS/PC, where RUN is hours
                                -- PER PIECE - the inverse - so that type would need `qty * O.RUN`. Both
                                -- HRS/PC rows here are rework carrying RUN = 0 and no master, so nothing is
                                -- lost today; a real HRS/PC standard would need its own branch.
                                --
                                -- ORIGINAL:
                                -- ELSE (O.RUN_HRS / O.CALC_START_QTY) * SUM(GOOD_QTY + LT.BAD_QTY)
                                --
                                -- ----------------------------------------------------
                                WHEN O.RUN_TYPE = 'PCS/HR' AND ISNULL(O.RUN, 0) > 0
                                THEN SUM(GOOD_QTY + LT.BAD_QTY)
                                     / CAST(O.RUN AS NUMERIC(19, 4))
                                ELSE 0
                            END 
                    END
                ELSE 0  
            END AS NUMERIC(19, 4)
        ) AS HOURS_MASTER,
        -- calculate the difference (delta) between actual hours worked and master hours
        -- HOURS_WORKED
        CAST(
            CASE 
                WHEN SR.USER_7 IN ('LATHING', 'MILLING', 'TBP CELL') OR EMP.DEPARTMENT_ID IN ('MEC', 'Fresatura', 'Stampi', 'Tornitura') 
                THEN 
                    CAST(SUM(ISNULL(LT.HOURS_WORKED, 0)) AS NUMERIC(19, 4)) 
                ELSE 
                    CAST(ISNULL(SUM(ISNULL(LT.HOURS_WORKED, 0)) / NULLIF(O.USER_5, 0), SUM(ISNULL(LT.HOURS_WORKED, 0))) AS NUMERIC(19, 4)) 
            END - 
            -- MINUS HOURS_MASTER
            CAST(
                CASE
                    -- see the note on the HOURS_MASTER branch above
                    WHEN LT.TYPE = 'S' THEN O.SETUP_HRS / ISNULL(NULLIF(O.USER_5, 0), 1)
                    WHEN LT.TYPE = 'R' THEN
                        CASE 
                            WHEN SUM(GOOD_QTY + LT.BAD_QTY) = 0 THEN 0  
                            ELSE 
                                CASE 
                                    WHEN O.RUN_TYPE = 'HRS/LOAD'  
                                    THEN CEILING(((SUM(GOOD_QTY + LT.BAD_QTY)) / O.LOAD_SIZE_QTY)) * O.RUN
                                    -- ---------------- CHANGED 2026-08-26 ----------------
                                    -- TO REVERT: uncomment the ORIGINAL line below and delete the three
                                    -- lines after it (WHEN / THEN / ELSE 0).
                                    -- Full rationale on the HOURS_MASTER branch above.
                                    --
                                    -- ORIGINAL:
                                    -- ELSE (O.RUN_HRS / O.CALC_START_QTY) * SUM(GOOD_QTY + LT.BAD_QTY)
                                    --
                                    -- ----------------------------------------------------
                                    WHEN O.RUN_TYPE = 'PCS/HR' AND ISNULL(O.RUN, 0) > 0
                                    THEN SUM(GOOD_QTY + LT.BAD_QTY)
                                         / CAST(O.RUN AS NUMERIC(19, 4))
                                    ELSE 0
                                END 
                        END
                    ELSE 0  
                END AS NUMERIC(19, 4)
            ) AS NUMERIC(19, 4)
        ) AS HOURS_DELTA

    FROM 
        dbo.LABOR_TICKET AS LT 
    INNER JOIN 
        dbo.WORK_ORDER AS WO 
        ON WO.BASE_ID = LT.WORKORDER_BASE_ID 
        AND WO.LOT_ID = LT.WORKORDER_LOT_ID 
        AND WO.SUB_ID = LT.WORKORDER_SUB_ID 
        AND WO.SPLIT_ID = LT.WORKORDER_SPLIT_ID 
    INNER JOIN 
        dbo.OPERATION AS O 
        ON O.WORKORDER_BASE_ID = LT.WORKORDER_BASE_ID 
        AND O.WORKORDER_LOT_ID = LT.WORKORDER_LOT_ID 
        AND O.WORKORDER_SPLIT_ID = LT.WORKORDER_SPLIT_ID 
        AND O.WORKORDER_SUB_ID = LT.WORKORDER_SUB_ID 
        AND LT.OPERATION_SEQ_NO = O.SEQUENCE_NO 
    INNER JOIN 
        dbo.PART AS P 
        ON WO.PART_ID = P.ID 
    INNER JOIN 
        dbo.SHOP_RESOURCE AS SR 
        ON SR.ID = LT.RESOURCE_ID 
    INNER JOIN 
        dbo.SHOP_RESOURCE_SITE AS SRS 
        ON SRS.RESOURCE_ID = SR.ID 
    INNER JOIN 
        dbo.EMPLOYEE AS EMP 
        ON EMP.ID = LT.EMPLOYEE_ID
    -- ADDED 2026-08-26. Counts the labour tickets DIRECTLY, keyed at the grain
    -- the outer query groups to: work order identity + operation sequence +
    -- resource + ticket type. RESOURCE_ID is in the key because the outer
    -- GROUP BY carries it - without it these would be whole-operation totals
    -- repeated on every resource row, which is the double-count
    -- fts_setup_view.py already warns about.
    --
    -- NOT keyed on TRANSACTION_DATE or DEPARTMENT_ID. Those are in the inner
    -- GROUP BY and are exactly what splits the tickets apart; counting across
    -- them is the entire point.
    --
    -- One row per key, and the key is a subset of LT's own grain, so this
    -- LEFT JOIN cannot fan out.
    LEFT JOIN (
        SELECT
            WORKORDER_BASE_ID, WORKORDER_LOT_ID, WORKORDER_SPLIT_ID,
            WORKORDER_SUB_ID, WORKORDER_TYPE, OPERATION_SEQ_NO,
            RESOURCE_ID, TYPE,
            COUNT(DISTINCT EMPLOYEE_ID) AS EMP_DISTINCT_TRUE,
            COUNT(*)                    AS LT_ROWS_TRUE
        FROM dbo.LABOR_TICKET
        -- MIRRORS THE OUTER WHERE, on the LT-side predicates only. Without
        -- this the counts would include tickets from before the window and
        -- 2-digit sequences, neither of which the analysis sees - so a row
        -- could report more employees than the evidence actually contains.
        -- The other predicates (O.STATUS, P.ID, WO.BASE_ID) sit on joined
        -- tables and decide whether the row exists at all, not which of its
        -- tickets count, so they do not belong here.
        -- KEEP THESE TWO IN STEP with the outer WHERE if the window moves.
        WHERE TRANSACTION_DATE >= '2025-10-01'
          AND TRANSACTION_DATE < CAST(GETDATE() AS DATE)
          AND LEN(CAST(OPERATION_SEQ_NO AS VARCHAR(10))) = 4
        GROUP BY
            WORKORDER_BASE_ID, WORKORDER_LOT_ID, WORKORDER_SPLIT_ID,
            WORKORDER_SUB_ID, WORKORDER_TYPE, OPERATION_SEQ_NO,
            RESOURCE_ID, TYPE
    ) AS LT_COUNT
        ON  LT_COUNT.WORKORDER_BASE_ID  = LT.WORKORDER_BASE_ID
        AND LT_COUNT.WORKORDER_LOT_ID   = LT.WORKORDER_LOT_ID
        AND LT_COUNT.WORKORDER_SPLIT_ID = LT.WORKORDER_SPLIT_ID
        AND LT_COUNT.WORKORDER_SUB_ID   = LT.WORKORDER_SUB_ID
        AND LT_COUNT.WORKORDER_TYPE     = LT.WORKORDER_TYPE
        AND LT_COUNT.OPERATION_SEQ_NO   = LT.OPERATION_SEQ_NO
        AND LT_COUNT.RESOURCE_ID        = LT.RESOURCE_ID
        AND LT_COUNT.TYPE               = LT.TYPE
    LEFT JOIN 
            dbo.USER_DEF_FIELDS AS UDF2
            ON UDF2.ID = 'UDF-0000021' 
            AND UDF2.PROGRAM_ID = 'VMPRTMNT' 
            AND UDF2.DOCUMENT_ID = P.ID
    -- ADDED: resolve the current master work order/operation for this part, so OPERATION_ROWID
    -- reflects the master routing rather than the transactional work order's own operation copy
    LEFT JOIN
        dbo.PART_SITE AS PS
        ON PS.PART_ID = P.ID
    LEFT JOIN
        dbo.WORK_ORDER AS WO_M
        ON WO_M.PART_ID = P.ID
        AND WO_M.LOT_ID = PS.ENGINEERING_MSTR
        AND WO_M.TYPE = 'M'
    LEFT JOIN
        dbo.OPERATION AS O_M
        ON O_M.WORKORDER_BASE_ID = WO_M.BASE_ID
        AND O_M.WORKORDER_LOT_ID = WO_M.LOT_ID
        AND O_M.WORKORDER_SPLIT_ID = WO_M.SPLIT_ID
        AND O_M.WORKORDER_SUB_ID = WO_M.SUB_ID
        AND O_M.WORKORDER_TYPE = WO_M.TYPE
        AND O_M.SEQUENCE_NO = LT.OPERATION_SEQ_NO
        -- the master slot must still hold the SAME operation. A routing edit
        -- overwrites the line in place, keeping its ROWID, so matching on
        -- sequence alone pairs an old work order with whatever occupies that
        -- slot today. Requiring the operation to agree makes a repurposed slot
        -- fall through to NULL, where the existing no-master exclusion drops it.
        AND O_M.OPERATION_TYPE = O.OPERATION_TYPE
    WHERE 
        O.STATUS = 'C'
        AND P.ID LIKE 'M.%'
        AND WO.BASE_ID LIKE '%OP%'
        --AND LT.TRANSACTION_DATE >= DATEFROMPARTS(
        --    CASE 
        --        WHEN MONTH(GETDATE()) >= 10 THEN YEAR(GETDATE()) - 1 
        --        ELSE YEAR(GETDATE()) - 2
        --    END, 10, 1)
        AND LT.TRANSACTION_DATE >= '2025-10-01'
        AND LT.TRANSACTION_DATE < CAST(GETDATE() AS DATE) 
        AND LEN(CAST(LT.OPERATION_SEQ_NO AS VARCHAR(10))) = 4
    GROUP BY 
        O_M.ROWID,
        LT.TRANSACTION_DATE,
        LT.TYPE,
        EMP.DEPARTMENT_ID,
        O.USER_5,
        O.OPERATION_TYPE,  
        O.RUN_TYPE,     
        O.RUN,
        O.RUN_HRS,
        O.LOAD_SIZE_QTY,
        O.CALC_START_QTY,
        LT.WORKORDER_BASE_ID, 
        WO.USER_8, 
        P.ID, 
        P.DESCRIPTION, 
        UDF2.STRING_VAL,
        LT.OPERATION_SEQ_NO,
        O.OPERATION_TYPE, 
        O.STATUS, 
        LT.RESOURCE_ID, 
        SR.DESCRIPTION, 
        SR.BID_RATE_CATEGORY_ID, 
        SR.USER_7, 
        O.SETUP_HRS,
        LT.WORKORDER_LOT_ID,
        LT.WORKORDER_SUB_ID,
        LT.WORKORDER_SPLIT_ID,
        LT.WORKORDER_TYPE
) AS ORIGINAL_QUERY

GROUP BY
    MASTER_OPS_ROWID,
    WORK_ORDER,
    WO_REV,
    FAMILY,
    PART_ID,
    PART_DESCRIPTION,
    PART_TYPE,
    OPERATION_SEQUENCE,
    OPERATION,
    OPERATION_TYPE,
    OPERATION_STATUS,
    RESOURCE_ID,
    RESOURCE_DESCRIPTION,
    ANALYTIC_SECTION,
    LT_TYPE,
    USER_5,
    RUN_TYPE_MASTER,
    RUN_MASTER,
    LOAD_SIZE_QTY_MASTER
ORDER BY
    FAMILY ASC,
    PART_ID ASC,
    WO_REV ASC,
    OPERATION_SEQUENCE ASC,
    WORK_ORDER ASC,
    LT_TYPE DESC
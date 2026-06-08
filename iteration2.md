# Iteration 2 Plan — coll-step1

Purpose: produce the initial collection report which is a list of collecttion to be made for a given beat

Scope
- Accept beat and salesman as input
- In staging area, generate a report of all pending vouchers(balance>0) for given beat and salesman
    - In case of no vouchers show "N0 Records Found"
    - Keep report data in json and actual report in .txt formatuse appropriate width and alignments for the report data
- add coll-step2 to the menu
  - when selected loop through each report in the stating
  - Open report in edit mode and way user to enter only payment. enable tab key for navigation between record. 
  - After last record edit ask user for confirmation and save data in json
  - After all payments are entered, display a summary showing total vouchers and total collections before final confirmation and save.

Files to produce
`staging/collyyyymmdd-<beat>-<salesman>.txt`
`staging/collyyyymmdd-<beat>-<salesman>.json`

Report format and layout
  - headers: `bill_no,date,balance,payment,beat,salesman`
  - details: `bill_no,balance,payment`
  - summary: `Total vouchers, Sum of coll`
  - notes:
    - `bill_no`: From voucher.
    - `date`: today
    - `balance`: from voucher for the given bill_no
    - `payment`: blank
    - `beat`: as per selection
    - `salesman`: as per selection


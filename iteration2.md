# Iteration 2 Plan — implement collections

Purpose: Implement collections workflow.

Collections workflow deals end to end, starting with salesman picking vouchers for collection, submitting collections to supervisor/distributor. Data is in staginging until this point. Final step in collection is supervisor/distributor confirmimg the collection. Here a batch update is make from staging to data.

Scope
- Accept beat and salesman as input
- In staging area, generate a report of all pending vouchers(balance>0) for given beat and salesman
    - In case of no vouchers show "N0 Records Found"
    - Keep report data in json and actual report in .txt format. use appropriate width and alignments for the report data
    - In the json maintain `stage` which tells what stage the report is in. It couild be stage1(picking vouchers for collection), stage2(sunission of collection), stage3(reconcilling of vouchers wth collection data)
    - Json should also maintain `isconfirmed`. This tells wether the supervisor/distributor has confirm the report. this is attached to every stage. ex [{"stage": "step1", "isconfirmed": "false"}, {"stage": "step2", "isconfirmed": "false"},{"stage": "step3", "isconfirmed": "false"}]
       
- add coll-step2 to the menu
  - when selected list available reports jsons and let the user pick. allow only one selection
  - when listing report jsons, mark jsons as ready when stage = step1 & isconfirmed = true
  - Open report in edit mode and allow user to enter only payment. enable tab key for navigation between record. 
  - After editing last record, ask user for save and then do a batch update to json in staging
  - While payments are being entered, display a running summary showing total vouchers and total collections at the bottom.

- Add confirm-step1 to the menu
  - when selected list available reports jsons and let the user pick. allow only one selection
  - when listing report jsons, mark jsons as ready when stage = step1 & isconfirmed = false
  - Present the text report and ask "Do you want to confirm?" is yes mark step1.isconfirm=ture

- Add confirm-step2 to the menu
  - when selected list available reports jsons and let the user pick. allow only one selection
  - when listing report jsons, mark jsons as ready when stage = step1 & isconfirmed = true
  - Present the text report and ask "Do you want to confirm?" is yes mark step2.isconfirm=ture
  

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

